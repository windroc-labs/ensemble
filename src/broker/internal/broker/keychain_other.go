//go:build !darwin

package broker

import "errors"

var errPlatform = errors.New("the broker's keychain and terminal access require macOS")

func CheckInstalled() error          { return errPlatform }
func ReadHidden() ([]byte, error)    { return nil, errPlatform }
func ReadHiddenTTY() ([]byte, error) { return nil, errPlatform }

type SystemKeychain struct{ Home string }

func (k SystemKeychain) Interactive() SystemKeychain { return k }

func (SystemKeychain) Get(string) ([]byte, error)                  { return nil, errPlatform }
func (SystemKeychain) GetWithDialog(string) ([]byte, error)        { return nil, errPlatform }
func (SystemKeychain) Put(string, []byte) error                    { return errPlatform }
func (SystemKeychain) AppAccount(string) (string, error)           { return "", errPlatform }
func (SystemKeychain) AppKey(string, string) ([]byte, error)       { return nil, errPlatform }
func (SystemKeychain) TrustApps(string, string, []byte) error      { return errPlatform }
func (SystemKeychain) TrustAppsByDialog(string, string) error      { return errPlatform }
func (SystemKeychain) TrustPartition(string, string, []byte) error { return errPlatform }
func (SystemKeychain) AppAccess(string, string) (AppAccess, error) { return AppAccess{}, errPlatform }
func (SystemKeychain) Secret(string, string) ([]byte, error)       { return nil, errPlatform }
func (SystemKeychain) PutSecret(string, string, []byte) error      { return errPlatform }
func (SystemKeychain) UpdateSecret(string, string, []byte) error   { return errPlatform }
func (SystemKeychain) DeleteSecret(string, string) error           { return errPlatform }
func (SystemKeychain) CreateCodex() error                          { return errPlatform }
func (SystemKeychain) WriteCodex([]byte) error                     { return errPlatform }
func (SystemKeychain) Accounts(string) ([]string, error)           { return nil, errPlatform }
func (SystemKeychain) Services(string) ([]string, error)           { return nil, errPlatform }
