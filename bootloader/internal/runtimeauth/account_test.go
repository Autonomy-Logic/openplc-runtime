// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

package runtimeauth

import (
	"context"
	"database/sql"
	"errors"
	"io"
	"log/slog"
	"path/filepath"
	"testing"
	"time"
)

// seedSchema creates a database with the given users DDL and inserts, returning its path.
func seedSchema(t *testing.T, ddl string, inserts ...string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "restapi.db")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatalf("creating db: %v", err)
	}
	defer db.Close()
	if _, err := db.Exec(ddl); err != nil {
		t.Fatalf("creating table: %v", err)
	}
	for _, insert := range inserts {
		if _, err := db.Exec(insert); err != nil {
			t.Fatalf("seeding: %v", err)
		}
	}
	return path
}

func openStore(t *testing.T, path string) *UserStore {
	t.Helper()
	store, err := OpenUserStore(path)
	if err != nil {
		t.Fatalf("opening store: %v", err)
	}
	t.Cleanup(func() { store.Close() })
	return store
}

func discardLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

const currentSchema = `CREATE TABLE users (
	id INTEGER PRIMARY KEY,
	username TEXT NOT NULL UNIQUE,
	password_hash TEXT NOT NULL,
	role TEXT NOT NULL DEFAULT 'engineer',
	sessions_valid_after_ms INTEGER NOT NULL DEFAULT 0)`

func TestNormalizeRole(t *testing.T) {
	cases := map[string]string{
		"":         RoleEngineer,
		"user":     RoleEngineer,
		"engineer": RoleEngineer,
		"admin":    RoleAdmin,
		"operator": RoleOperator,
		"viewer":   RoleViewer,
	}
	for stored, want := range cases {
		if got := NormalizeRole(stored); got != want {
			t.Errorf("NormalizeRole(%q) = %q, want %q", stored, got, want)
		}
	}
}

func TestOnlyAdminAndEngineerMayUseTheBootloader(t *testing.T) {
	cases := map[string]bool{
		RoleAdmin: true, RoleEngineer: true, RoleOperator: false, RoleViewer: false,
		"user": false, "": false, "superuser": false,
	}
	for role, want := range cases {
		if got := MayUseBootloader(role); got != want {
			t.Errorf("MayUseBootloader(%q) = %v, want %v", role, got, want)
		}
	}
}

func TestAStoredUserRoleIsReadAsEngineer(t *testing.T) {
	store := openStore(t, seedSchema(t, currentSchema,
		"INSERT INTO users VALUES (4, 'eng', '"+vectorHash+"', 'user', 0)"))

	user, err := store.FindUser(context.Background(), "eng")
	if err != nil {
		t.Fatalf("finding user: %v", err)
	}
	if user.Role != RoleEngineer {
		t.Fatalf("FindUser: want engineer, got %q", user.Role)
	}
	account, err := store.AccountByID(context.Background(), "4")
	if err != nil {
		t.Fatalf("reading account: %v", err)
	}
	if account.Role != RoleEngineer {
		t.Fatalf("AccountByID: want engineer, got %q", account.Role)
	}
}

func TestAccountByIDReadsTheRoleAndCutOff(t *testing.T) {
	store := openStore(t, seedSchema(t, currentSchema,
		"INSERT INTO users VALUES (5, 'op', 'x', 'operator', 1788465200123)"))

	account, err := store.AccountByID(context.Background(), "5")
	if err != nil {
		t.Fatalf("reading account: %v", err)
	}
	if account.Role != RoleOperator || account.SessionsValidAfterMs != 1788465200123 {
		t.Fatalf("got %+v", account)
	}
}

func TestADatabaseWithoutTheCutOffColumnReadsAsZero(t *testing.T) {
	// A database the runtime has not booted on since the column was added.
	store := openStore(t, seedSchema(t, `CREATE TABLE users (
		id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, role TEXT)`,
		"INSERT INTO users VALUES (2, 'eng', 'x', 'engineer')"))

	account, err := store.AccountByID(context.Background(), "2")
	if err != nil {
		t.Fatalf("reading account: %v", err)
	}
	if account.SessionsValidAfterMs != 0 || account.Role != RoleEngineer {
		t.Fatalf("got %+v, want engineer with cut-off 0", account)
	}
}

func TestADatabaseWithoutTheRoleColumnReadsAsAdmin(t *testing.T) {
	// The runtime's migration adds the role column with default admin.
	store := openStore(t, seedSchema(t, `CREATE TABLE users (
		id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT)`,
		"INSERT INTO users VALUES (1, 'old', '"+vectorHash+"')"))

	user, err := store.FindUser(context.Background(), "old")
	if err != nil {
		t.Fatalf("finding user: %v", err)
	}
	if user.Role != RoleAdmin {
		t.Fatalf("FindUser: want admin, got %q", user.Role)
	}
	account, err := store.AccountByID(context.Background(), "1")
	if err != nil {
		t.Fatalf("reading account: %v", err)
	}
	if account.Role != RoleAdmin || account.SessionsValidAfterMs != 0 {
		t.Fatalf("AccountByID: got %+v", account)
	}
}

func TestAccountByIDOfADeletedAccountIsNoSuchUser(t *testing.T) {
	store := openStore(t, seedSchema(t, currentSchema))
	if _, err := store.AccountByID(context.Background(), "9"); !errors.Is(err, ErrNoSuchUser) {
		t.Fatalf("want ErrNoSuchUser, got %v", err)
	}
}

func TestAccountByIDWithoutAUsersTableIsNoSuchUser(t *testing.T) {
	store := openStore(t, seedDB(t, false))
	if _, err := store.AccountByID(context.Background(), "1"); !errors.Is(err, ErrNoSuchUser) {
		t.Fatalf("want ErrNoSuchUser, got %v", err)
	}
}

func TestANilStoreAccountByIDReportsNoDatabase(t *testing.T) {
	var store *UserStore
	if _, err := store.AccountByID(context.Background(), "1"); !errors.Is(err, ErrNoDatabase) {
		t.Fatalf("want ErrNoDatabase, got %v", err)
	}
}

func TestTheProviderReadsTheAccountFromTheDataDirectory(t *testing.T) {
	dir := t.TempDir()
	db, err := sql.Open("sqlite", filepath.Join(dir, "restapi.db"))
	if err != nil {
		t.Fatalf("creating db: %v", err)
	}
	if _, err := db.Exec(currentSchema); err != nil {
		t.Fatalf("creating table: %v", err)
	}
	if _, err := db.Exec("INSERT INTO users VALUES (3, 'v', 'x', 'viewer', 42)"); err != nil {
		t.Fatalf("seeding: %v", err)
	}
	db.Close()

	provider := NewProvider(dir, discardLogger())
	defer provider.Close()
	account, err := provider.AccountByID(context.Background(), "3")
	if err != nil {
		t.Fatalf("reading account: %v", err)
	}
	if account.Role != RoleViewer || account.SessionsValidAfterMs != 42 {
		t.Fatalf("got %+v", account)
	}
}

// --- token cut-off -------------------------------------------------------

func TestAnIssuedTokenCarriesItsIssueTimeInMilliseconds(t *testing.T) {
	before := time.Now().UnixMilli()
	token, err := IssueToken(vectorSecret, "7", time.Hour)
	if err != nil {
		t.Fatalf("issuing: %v", err)
	}
	after := time.Now().UnixMilli()
	claims, err := VerifyToken(vectorSecret, token)
	if err != nil {
		t.Fatalf("verifying: %v", err)
	}
	if claims.IssuedAtMs < before || claims.IssuedAtMs > after {
		t.Fatalf("iat_ms %d is outside [%d, %d]", claims.IssuedAtMs, before, after)
	}
}

func TestIssuedBeforeComparesWithTheCutOff(t *testing.T) {
	cases := []struct {
		name     string
		issuedMs int64
		cutoffMs int64
		want     bool
	}{
		{"no cut-off", 1000, 0, false},
		{"issued before the cut-off", 999, 1000, true},
		{"issued at the cut-off", 1000, 1000, true},
		{"issued after the cut-off", 1001, 1000, false},
		{"no iat_ms and no cut-off", 0, 0, false},
		{"no iat_ms with a cut-off", 0, 1000, true},
	}
	for _, tc := range cases {
		claims := Claims{IssuedAtMs: tc.issuedMs}
		if got := claims.IssuedBefore(tc.cutoffMs); got != tc.want {
			t.Errorf("%s: got %v, want %v", tc.name, got, tc.want)
		}
	}
}

func TestATokenWithoutIssueTimeIsRefusedOnlyOnceACutOffIsSet(t *testing.T) {
	now := time.Now().Unix()
	token := signedToken(t, vectorSecret, Claims{
		Subject: "7", Type: TokenType, IssuedAt: now, NotBefore: now,
		Expires: now + 3600, JTI: "x", Audience: Audience,
	})
	claims, err := VerifyToken(vectorSecret, token)
	if err != nil {
		t.Fatalf("verifying: %v", err)
	}
	if claims.IssuedBefore(0) {
		t.Error("refused with no cut-off")
	}
	if !claims.IssuedBefore(1) {
		t.Error("accepted with a cut-off set")
	}
}
