package runtimeauth

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/url"
	"strings"

	_ "modernc.org/sqlite" // pure-Go SQLite driver: no cgo, cross-compiles
)

// The runtime's users table, from webserver/restapi.py::User.
//
// Read-only, and opened read-only. The bootloader authenticates against these
// accounts but must never create, modify or promote one -- user management
// stays entirely in the runtime, including the first-user bootstrap. A bootloader
// that could write here would be a second, less-reviewed path to an admin
// account on the device.
const (
	usersTable  = "users"
	openTimeout = 5 * 1000 // busy_timeout, milliseconds
)

// ErrNoSuchUser is returned when the username is absent. Callers must answer
// the same 401 they would for a bad password: distinguishing the two tells an
// unauthenticated caller which usernames exist.
var ErrNoSuchUser = errors.New("no such user")

// ErrNoUsers means the runtime has never had an account created.
//
// The bootloader refuses every command in that state, deliberately. First-user
// bootstrap is a sensitive flow and it lives in the runtime alone; duplicating
// it here would mean two places that can mint the first admin on a device.
// The practical consequence is narrow: it only bites if the very first runtime
// start fails before anyone has logged in, and install.sh runs with shell
// access anyway.
var ErrNoUsers = errors.New("no users have been created yet")

// User is the subset of an account the bootloader needs.
type User struct {
	ID           string
	Username     string
	PasswordHash string
	Role         string
}

// UserStore reads accounts from the runtime's SQLite database.
type UserStore struct {
	db *sql.DB
}

// OpenUserStore opens the runtime database read-only.
//
// mode=ro is what makes a read-only bind mount work: SQLite would otherwise
// want to create a rollback journal beside the file and fail on the mount
// rather than on the query. immutable is NOT set -- the runtime writes to this
// database while we read it, and immutable would tell SQLite the file can
// never change, which would serve stale pages after a password change.
func OpenUserStore(dbPath string) (*UserStore, error) {
	dsn := fmt.Sprintf("file:%s?mode=ro&_pragma=busy_timeout(%d)",
		url.PathEscape(dbPath), openTimeout)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("opening runtime database %s: %w", dbPath, err)
	}
	// A single connection: the read volume is one query per login, and
	// SQLite's concurrency story is better served by not opening several
	// readers against a file another process is writing.
	db.SetMaxOpenConns(1)
	return &UserStore{db: db}, nil
}

// Close releases the database handle.
func (s *UserStore) Close() error {
	if s == nil || s.db == nil {
		return nil
	}
	return s.db.Close()
}

// CountUsers reports how many accounts exist.
//
// Used to answer "is this device bootstrapped". A missing table counts as
// zero rather than an error: a runtime that has never started leaves the file
// present but empty, and that is the no-users case, not a broken database.
func (s *UserStore) CountUsers(ctx context.Context) (int, error) {
	var count int
	query := "SELECT COUNT(*) FROM " + usersTable
	if err := s.db.QueryRowContext(ctx, query).Scan(&count); err != nil {
		if isMissingTable(err) {
			return 0, nil
		}
		return 0, fmt.Errorf("counting users: %w", err)
	}
	return count, nil
}

// FindUser looks up an account by username.
func (s *UserStore) FindUser(ctx context.Context, username string) (*User, error) {
	query := "SELECT id, username, password_hash, role FROM " + usersTable + " WHERE username = ?"
	row := s.db.QueryRowContext(ctx, query, username)

	var user User
	// role is nullable in databases that predate the RBAC column, which the
	// runtime migrates in place; scanning into a NullString keeps a
	// half-migrated device usable instead of failing every login.
	var role sql.NullString
	if err := row.Scan(&user.ID, &user.Username, &user.PasswordHash, &role); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrNoSuchUser
		}
		if isMissingTable(err) {
			return nil, ErrNoUsers
		}
		return nil, fmt.Errorf("looking up user: %w", err)
	}
	// The runtime defaults this column to admin precisely because the
	// pre-RBAC runtime treated every account as an admin; matching that
	// avoids silently demoting an existing operator.
	user.Role = role.String
	if user.Role == "" {
		user.Role = "admin"
	}
	return &user, nil
}

// Authenticate verifies a username and password, returning the account.
//
// Both a missing user and a bad password come back as ErrNoSuchUser so the
// caller cannot accidentally answer differently for the two. The password is
// still hashed for an unknown user -- see below -- so the two paths cost
// roughly the same time.
func (s *UserStore) Authenticate(ctx context.Context, username, password, pepper string) (*User, error) {
	user, err := s.FindUser(ctx, username)
	if err != nil {
		if errors.Is(err, ErrNoSuchUser) {
			// Hash against a throwaway value so an unknown username does not
			// return noticeably faster than a known one with a wrong password.
			// Without this, response timing enumerates valid accounts.
			_, _ = VerifyPassword(dummyHash, password, pepper)
		}
		return nil, err
	}

	ok, verifyErr := VerifyPassword(user.PasswordHash, password, pepper)
	if verifyErr != nil {
		// A hash we cannot parse is a deployment problem, not a wrong
		// password, and saying so is what makes it fixable.
		return nil, verifyErr
	}
	if !ok {
		return nil, ErrNoSuchUser
	}
	return user, nil
}

// dummyHash is a real 600k-iteration PBKDF2 hash of a value nobody knows,
// used only to spend comparable time on an unknown username.
const dummyHash = "pbkdf2:sha256:600000$KMV1LlY0aXBhZGRpbmc$" +
	"0000000000000000000000000000000000000000000000000000000000000000"

// isMissingTable spots the driver's "no such table" error. Matched on the
// message because modernc's SQLite maps it to a generic error value rather
// than a distinguishable sentinel.
func isMissingTable(err error) bool {
	if err == nil {
		return false
	}
	return strings.Contains(strings.ToLower(err.Error()), "no such table")
}
