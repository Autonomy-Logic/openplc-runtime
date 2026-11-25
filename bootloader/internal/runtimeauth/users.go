// SPDX-License-Identifier: GPL-3.0-only
// Copyright (c) 2026 Autonomy®

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

// Users table from the runtime (webserver/restapi.py::User), opened
// read-only. User management (including first-user bootstrap) stays in
// the runtime; the bootloader never writes.
const (
	usersTable  = "users"
	openTimeout = 5 * 1000 // busy_timeout, milliseconds
)

// ErrNoSuchUser is returned when the username is absent. Callers must answer
// the same 401 they would for a bad password: distinguishing the two tells an
// unauthenticated caller which usernames exist.
var ErrNoSuchUser = errors.New("no such user")

// ErrNoUsers means no account has been created. Bootloader refuses every
// command in that state so it cannot mint a first admin.
var ErrNoUsers = errors.New("no users have been created yet")

// ErrNoDatabase: the runtime has never started so restapi.db is absent.
// A nil UserStore is a legitimate state; every method tolerates a nil
// receiver (typed nil in an interface is not nil at the call site).
var ErrNoDatabase = errors.New("the runtime account database is not available")

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

// OpenUserStore opens the runtime database read-only. mode=ro so SQLite
// does not try to create a rollback journal. immutable is NOT set since
// the runtime writes while we read (password change).
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

// CountUsers reports how many accounts exist. A missing table counts as
// zero: a never-started runtime leaves the file empty, not broken.
func (s *UserStore) CountUsers(ctx context.Context) (int, error) {
	if s == nil || s.db == nil {
		return 0, ErrNoDatabase
	}
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
	if s == nil || s.db == nil {
		return nil, ErrNoDatabase
	}
	columns, err := s.accountColumns(ctx)
	if err != nil {
		return nil, err
	}
	query := "SELECT id, username, password_hash, " + columns.role + " FROM " + usersTable + " WHERE username = ?"
	row := s.db.QueryRowContext(ctx, query, username)

	var user User
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
	user.Role = NormalizeRole(role.String)
	return &user, nil
}

// Authenticate verifies username and password. Both missing user and
// bad password return ErrNoSuchUser; the dummy hash below equalises
// timing so an unknown username does not return faster.
func (s *UserStore) Authenticate(ctx context.Context, username, password, pepper string) (*User, error) {
	if s == nil || s.db == nil {
		return nil, ErrNoDatabase
	}
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

// Account is what the bootloader reads about a token's account on every request.
type Account struct {
	Role string
	// SessionsValidAfterMs is the account cut-off in ms since the epoch; tokens issued before it are refused.
	SessionsValidAfterMs int64
}

// AccountByID reads the role and cut-off of an account per request, so a change applies at once.
func (s *UserStore) AccountByID(ctx context.Context, userID string) (*Account, error) {
	if s == nil || s.db == nil {
		return nil, ErrNoDatabase
	}
	columns, err := s.accountColumns(ctx)
	if err != nil {
		return nil, err
	}
	query := "SELECT " + columns.role + ", " + columns.validAfter + " FROM " + usersTable + " WHERE id = ?"
	var role sql.NullString
	var validAfter sql.NullInt64
	if err := s.db.QueryRowContext(ctx, query, userID).Scan(&role, &validAfter); err != nil {
		if errors.Is(err, sql.ErrNoRows) || isMissingTable(err) {
			return nil, ErrNoSuchUser
		}
		return nil, fmt.Errorf("reading the account %q: %w", userID, err)
	}
	return &Account{Role: NormalizeRole(role.String), SessionsValidAfterMs: validAfter.Int64}, nil
}

// columnExprs are the select expressions for columns an older users table may lack.
type columnExprs struct {
	role       string
	validAfter string
}

// accountColumns picks the expression for each optional column: the column, or the value the runtime's migration gives it.
func (s *UserStore) accountColumns(ctx context.Context) (columnExprs, error) {
	rows, err := s.db.QueryContext(ctx, "SELECT name FROM pragma_table_info('"+usersTable+"')")
	if err != nil {
		return columnExprs{}, fmt.Errorf("reading the users table's columns: %w", err)
	}
	defer rows.Close()
	present := map[string]bool{}
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return columnExprs{}, fmt.Errorf("reading the users table's columns: %w", err)
		}
		present[name] = true
	}
	if err := rows.Err(); err != nil {
		return columnExprs{}, fmt.Errorf("reading the users table's columns: %w", err)
	}
	exprs := columnExprs{role: "'" + RoleAdmin + "'", validAfter: "0"}
	if present["role"] {
		exprs.role = "role"
	}
	if present["sessions_valid_after_ms"] {
		exprs.validAfter = "sessions_valid_after_ms"
	}
	return exprs, nil
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
