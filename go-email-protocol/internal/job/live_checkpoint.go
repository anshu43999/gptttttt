package job

import (
	"context"
	"encoding/json"

	"github.com/gpt-register/go-email-protocol/internal/cryptostore"
	"github.com/gpt-register/go-email-protocol/internal/ledger"
	"github.com/gpt-register/go-email-protocol/internal/protocol"
	"github.com/gpt-register/go-email-protocol/internal/transport"
)

// sealLiveOTPCheckpoint writes cookies + live cursor into encrypted secret_blob.
// Enables worker restart while waiting_for_otp without full re-auth.
func (m *Manager) sealLiveOTPCheckpoint(rt *Runtime, cur protocol.Cursor) error {
	if m == nil || m.crypto == nil || rt == nil {
		return nil
	}
	var jarJSON []byte
	if rt.Client != nil {
		if cio, ok := rt.Client.(transport.CookieIO); ok {
			if b, err := cio.ExportCookies(); err == nil {
				jarJSON = b
			}
		}
	}
	// Cursor without OTP/password/sentinel secrets for resume.
	resume := protocol.Cursor{
		State:       cur.State,
		ContinueURL: cur.ContinueURL,
		DeviceID:    cur.DeviceID,
		CSRF:        cur.CSRF,
		Email:       cur.Email,
	}
	curJSON, _ := json.Marshal(resume)

	rec, err := m.led.GetByID(context.Background(), rt.JobID)
	if err != nil {
		return err
	}
	sec := cryptostore.Secrets{
		Password:   rt.password,
		Capability: rt.capability,
		BridgeCap:  rt.bridgeCap,
	}
	if len(rec.SecretBlob) > 0 {
		if old, oerr := m.crypto.Open(rec.SecretBlob); oerr == nil {
			if sec.Password == "" {
				sec.Password = old.Password
			}
			if sec.Capability == "" {
				sec.Capability = old.Capability
			}
			if sec.BridgeCap == "" {
				sec.BridgeCap = old.BridgeCap
			}
			if old.AccessToken != "" {
				sec.AccessToken = old.AccessToken
			}
		}
	}
	sec.CookieJarJSON = jarJSON
	if sec.Extra == nil {
		sec.Extra = map[string]string{}
	}
	sec.Extra["live_cursor"] = string(curJSON)
	sec.Extra["live_checkpoint"] = "1"
	blob, err := m.crypto.Seal(sec)
	if err != nil {
		return err
	}
	_, err = m.led.BumpVersion(context.Background(), rt.JobID, func(r *ledger.Record) error {
		r.SecretBlob = blob
		return nil
	})
	return err
}

// restoreLiveOTPCheckpoint rebuilds live engine from secret_blob after worker restart.
func (m *Manager) restoreLiveOTPCheckpoint(rt *Runtime) bool {
	if m == nil || m.crypto == nil || rt == nil {
		return false
	}
	rec, err := m.led.GetByID(context.Background(), rt.JobID)
	if err != nil || len(rec.SecretBlob) == 0 {
		return false
	}
	sec, err := m.crypto.Open(rec.SecretBlob)
	if err != nil {
		return false
	}
	if sec.Extra["live_checkpoint"] != "1" {
		return false
	}
	var cur protocol.Cursor
	if raw := sec.Extra["live_cursor"]; raw != "" {
		_ = json.Unmarshal([]byte(raw), &cur)
	}
	if cur.Email == "" {
		cur.Email = rt.email
	}
	if cur.State == "" {
		cur.State = protocol.S9
	}
	if len(sec.CookieJarJSON) > 0 && rt.Client != nil {
		if cio, ok := rt.Client.(transport.CookieIO); ok {
			_ = cio.ImportCookies(sec.CookieJarJSON)
		}
	}
	eng := &protocol.Engine{
		Mode:     protocol.ModeLive,
		Bundle:   rt.Bundle,
		Client:   rt.Client,
		Email:    rt.email,
		Password: rt.password,
	}
	rt.mu.Lock()
	rt.liveEng = eng
	rt.liveCur = cur
	rt.mu.Unlock()
	return true
}
