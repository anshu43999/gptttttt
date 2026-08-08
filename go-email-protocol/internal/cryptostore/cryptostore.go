// Package cryptostore encrypts secret checkpoint blobs for the durable ledger.
// Plain password/OTP/capability must not appear in SQLite columns.
package cryptostore

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

// Secrets is the in-memory secret material for a job.
type Secrets struct {
	Password            string            `json:"password,omitempty"`
	Capability          string            `json:"capability,omitempty"`
	BridgeCap           string            `json:"bridge_capability,omitempty"`
	OTPCode             string            `json:"otp_code,omitempty"`
	AccessToken         string            `json:"access_token,omitempty"`
	MailboxClientID     string            `json:"mailbox_client_id,omitempty"`
	MailboxRefreshToken string            `json:"mailbox_refresh_token,omitempty"`
	CookieJarJSON       []byte            `json:"cookie_jar_json,omitempty"`
	Extra               map[string]string `json:"extra,omitempty"`
}

// Store encrypts/decrypts Secrets with AES-GCM.
type Store struct {
	gcm cipher.AEAD
}

// NewFromKey derives an AES-256-GCM key from raw key material (any length).
func NewFromKey(keyMaterial []byte) (*Store, error) {
	if len(keyMaterial) == 0 {
		return nil, errors.New("empty key material")
	}
	sum := sha256.Sum256(keyMaterial)
	block, err := aes.NewCipher(sum[:])
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	return &Store{gcm: gcm}, nil
}

// NewRandomKey creates a store with a random 32-byte key (returned for tests/persist).
func NewRandomKey() (*Store, []byte, error) {
	key := make([]byte, 32)
	if _, err := io.ReadFull(rand.Reader, key); err != nil {
		return nil, nil, err
	}
	s, err := NewFromKey(key)
	return s, key, err
}

// Seal encrypts secrets to ciphertext (nonce||ct).
func (s *Store) Seal(sec Secrets) ([]byte, error) {
	if s == nil || s.gcm == nil {
		return nil, errors.New("nil store")
	}
	plain, err := json.Marshal(sec)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, s.gcm.NonceSize())
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return nil, err
	}
	ct := s.gcm.Seal(nonce, nonce, plain, nil)
	return ct, nil
}

// Open decrypts ciphertext into Secrets.
func (s *Store) Open(blob []byte) (Secrets, error) {
	var zero Secrets
	if s == nil || s.gcm == nil {
		return zero, errors.New("nil store")
	}
	if len(blob) < s.gcm.NonceSize() {
		return zero, errors.New("ciphertext too short")
	}
	nonce, ct := blob[:s.gcm.NonceSize()], blob[s.gcm.NonceSize():]
	plain, err := s.gcm.Open(nil, nonce, ct, nil)
	if err != nil {
		return zero, fmt.Errorf("decrypt: %w", err)
	}
	var sec Secrets
	if err := json.Unmarshal(plain, &sec); err != nil {
		return zero, err
	}
	return sec, nil
}
