package headerpreset

import (
	"crypto/rand"
	"encoding/hex"
)

func randomHex(nBytes int) string {
	b := make([]byte, nBytes)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
