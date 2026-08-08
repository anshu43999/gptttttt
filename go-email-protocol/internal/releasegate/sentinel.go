package releasegate

import (
	"fmt"

	"github.com/gpt-register/go-email-protocol/internal/sentinel"
)

// SentinelEvidenceFromManifest derives the explicit release identity carried by
// gate/checkpoint metadata from a validated immutable manifest. The release ID
// and manifest hash remain separate from fingerprint Bundle consistency.
func SentinelEvidenceFromManifest(manifest *sentinel.ReleaseManifest, approved bool) (SentinelEvidence, error) {
	if manifest == nil {
		return SentinelEvidence{}, fmt.Errorf("releasegate: nil Sentinel release manifest")
	}
	if err := manifest.ValidateEmbeddedPin(); err != nil {
		return SentinelEvidence{}, fmt.Errorf("releasegate: validate Sentinel release: %w", err)
	}
	loader := manifest.Loader()
	sdk := manifest.SDK()
	return SentinelEvidence{
		ReleaseID:       manifest.ReleaseID(),
		ManifestSHA256: manifest.ManifestSHA256(),
		LoaderSHA256:   loader.SHA256,
		SDKSHA256:      sdk.SHA256,
		Approved:       approved,
		Complete:       true,
	}, nil
}
