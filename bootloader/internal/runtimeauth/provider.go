package runtimeauth

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
)

// Provider serves the runtime's credentials, reloading them when they change.
//
// Loading once at start-up was wrong in the case that matters most: on a fresh
// install the bootloader starts BEFORE the runtime has ever run, so neither
// `.env` nor `restapi.db` exists yet. Every authenticated route then answered
// 503 until someone restarted the bootloader container -- while
// /capabilities answered happily, so the editor offered "Change runtime
// version" and the login behind it failed. The same staleness applies whenever
// the runtime regenerates its secrets.
//
// So the files are re-examined on use. A stat of each per request is cheap
// next to the PBKDF2 verification it precedes, and it means the bootloader
// becomes usable the moment the runtime has written them, with no restart.
type Provider struct {
	dataDir string
	log     *slog.Logger

	mu      sync.Mutex
	secrets *Secrets
	users   *UserStore
	// Fingerprints of the files behind the cache above, so a reload happens
	// when they are replaced and not on every call.
	secretsAt fileStamp
	usersAt   fileStamp
}

// fileStamp is what "the file changed" means here: a different size,
// modification time or inode. Cheap to take and enough to catch a rewrite,
// including one that preserves the length.
type fileStamp struct {
	present bool
	size    int64
	modUnix int64
	inode   uint64
}

func stamp(path string) fileStamp {
	info, err := os.Stat(path)
	if err != nil {
		return fileStamp{}
	}
	return fileStamp{
		present: true,
		size:    info.Size(),
		modUnix: info.ModTime().UnixNano(),
		inode:   inodeOf(info),
	}
}

// NewProvider returns a Provider for the runtime's data directory. It reads
// nothing yet: a device whose runtime has never started has nothing to read,
// and refusing to boot would leave nothing listening on the device that most
// needs a way in.
func NewProvider(dataDir string, log *slog.Logger) *Provider {
	return &Provider{dataDir: dataDir, log: log, secrets: &Secrets{}}
}

func (p *Provider) envPath() string { return filepath.Join(p.dataDir, ".env") }
func (p *Provider) dbPath() string  { return filepath.Join(p.dataDir, "restapi.db") }

// refresh reloads whatever has appeared or changed since the last look.
// Called with p.mu held.
func (p *Provider) refresh() {
	if current := stamp(p.envPath()); current != p.secretsAt {
		p.secretsAt = current
		if secrets, err := LoadSecrets(p.envPath()); err != nil {
			// Not an error worth shouting about on a fresh device: the runtime
			// has simply not written it yet, and the next request looks again.
			p.secrets = &Secrets{}
			p.log.Debug("runtime secrets unavailable", "error", err)
		} else {
			p.secrets = secrets
			p.log.Info("loaded the runtime's secrets", "path", p.envPath())
		}
	}

	if current := stamp(p.dbPath()); current != p.usersAt {
		p.usersAt = current
		if p.users != nil {
			p.users.Close()
			p.users = nil
		}
		if users, err := OpenUserStore(p.dbPath()); err != nil {
			p.log.Debug("runtime account database unavailable", "error", err)
		} else {
			p.users = users
			p.log.Info("opened the runtime's account database", "path", p.dbPath())
		}
	}
}

// Secrets returns the runtime's signing secret and pepper, reloading first.
// Never nil: an empty Secrets makes token verification fail closed.
func (p *Provider) Secrets() *Secrets {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.refresh()
	return p.secrets
}

// Authenticate resolves credentials against the runtime's account set.
func (p *Provider) Authenticate(
	ctx context.Context, username, password, pepper string,
) (*User, error) {
	p.mu.Lock()
	p.refresh()
	users := p.users
	p.mu.Unlock()
	return users.Authenticate(ctx, username, password, pepper)
}

// CountUsers reports how many accounts the runtime has.
func (p *Provider) CountUsers(ctx context.Context) (int, error) {
	p.mu.Lock()
	p.refresh()
	users := p.users
	p.mu.Unlock()
	return users.CountUsers(ctx)
}

// RoleByID reports the role of the account a token was issued for.
func (p *Provider) RoleByID(ctx context.Context, userID string) (string, error) {
	p.mu.Lock()
	p.refresh()
	users := p.users
	p.mu.Unlock()
	return users.RoleByID(ctx, userID)
}

// Close releases the database handle.
func (p *Provider) Close() {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.users != nil {
		p.users.Close()
		p.users = nil
	}
}
