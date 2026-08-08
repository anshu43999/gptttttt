package protocol_test

import (
	"testing"

	"github.com/gpt-register/go-email-protocol/internal/protocol"
)

func TestRequiredStateIDs(t *testing.T) {
	ids := protocol.RequiredStateIDs()
	if len(ids) != 29 {
		t.Fatalf("got %d want 29", len(ids))
	}
	for _, id := range ids {
		if protocol.KindOf(id) == "" {
			t.Errorf("KindOf empty for %s", id)
		}
	}
}
