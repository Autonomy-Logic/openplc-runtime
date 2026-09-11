package updater

import (
	"errors"
	"net/http"
	"strings"
	"testing"

	"github.com/Autonomy-Logic/openplc-runtime/bootloader/internal/dockerapi"
)

// The failure an operator actually hit: a device installed from a side-loaded
// image kept a bare repository name in its spec, so every pull went to Docker
// Hub. The tag was real; the message blamed the tag.
func TestDescribePullFailureNamesTheUnqualifiedRepository(t *testing.T) {
	err := &dockerapi.APIError{
		Status:  http.StatusInternalServerError,
		Path:    "/images/create?fromImage=openplc-runtime&tag=v4.1.10",
		Message: "pull access denied for openplc-runtime, repository does not exist",
	}

	got := describePullFailure("openplc-runtime", "openplc-runtime:v4.1.10", err)

	if !strings.Contains(got, "pull access denied") {
		t.Errorf("the daemon's own reason was dropped: %q", got)
	}
	if !strings.Contains(got, "Docker Hub") {
		t.Errorf("did not explain where the download went: %q", got)
	}
	// The API path is machinery, and putting it in front of an operator sends
	// them looking at the wrong layer.
	if strings.Contains(got, "/images/create") {
		t.Errorf("leaked the Docker API path: %q", got)
	}
}

func TestDescribePullFailureStaysShortForAProperRepository(t *testing.T) {
	err := &dockerapi.APIError{
		Status:  http.StatusNotFound,
		Path:    "/images/create",
		Message: "manifest unknown",
	}

	got := describePullFailure(
		"ghcr.io/autonomy-logic/openplc-runtime",
		"ghcr.io/autonomy-logic/openplc-runtime:v9.9.9", err)

	want := "could not download ghcr.io/autonomy-logic/openplc-runtime:v9.9.9: manifest unknown"
	if got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestIsUnqualifiedRepository(t *testing.T) {
	cases := map[string]bool{
		"openplc-runtime":                        true,
		"autonomylogic/openplc-runtime":          true, // a Docker Hub namespace
		"ghcr.io/autonomy-logic/openplc-runtime": false,
		"localhost:5000/openplc-runtime":         false,
		"registry.local:5000/openplc":            false,
	}
	for repository, want := range cases {
		if got := isUnqualifiedRepository(repository); got != want {
			t.Errorf("%q: got %v, want %v", repository, got, want)
		}
	}
}

// A transport failure carries no APIError, and the innermost segment is still
// the only part worth showing.
func TestReasonFallsBackToTheInnermostSegment(t *testing.T) {
	err := errors.New("could not download x: pulling x: connection refused")
	if got := dockerapi.Reason(err); got != "connection refused" {
		t.Errorf("got %q", got)
	}
}
