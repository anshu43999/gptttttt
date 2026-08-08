package protocol

import (
	"testing"

	"github.com/gpt-register/go-email-protocol/internal/headerpreset"
)

func TestS0S3SpecsComplete(t *testing.T) {
	specs := S0S3Specs()
	if len(specs) != 4 {
		t.Fatalf("len=%d", len(specs))
	}
	if specs[1].Preset != headerpreset.DocumentNavigation {
		t.Fatal(specs[1].Preset)
	}
	if specs[3].Request.Method != "POST" {
		t.Fatal(specs[3].Request.Method)
	}
	if SpecFor(S3) == nil || SpecFor(S10) == nil {
		t.Fatal("SpecFor")
	}
	if PresetFor(S3) != headerpreset.SameOriginFetch {
		t.Fatal(PresetFor(S3))
	}
}
