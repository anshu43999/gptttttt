package sentinel

import (
	"bytes"
	"encoding/json"
	"errors"
	"math/rand/v2"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/gpt-register/go-email-protocol/internal/fingerprint"
)

const verifiedManifestSHA256 = "sha256:938f2cf548e273bb893db207b39a52876440378a1a4022fdbf84253b101c1f69"

func releaseFixturePath(t *testing.T) string {
	t.Helper()
	return filepath.Join("..", "..", "testdata", "rechallenge", "sentinel-releases", "20260219f9f6-r1", "manifest.json")
}

func loadReleaseFixture(t *testing.T) *ReleaseManifest {
	t.Helper()
	release, err := LoadRelease(releaseFixturePath(t))
	if err != nil {
		t.Fatalf("LoadRelease: %v", err)
	}
	return release
}

func TestReleaseManifestLoadsVerifiedContentIdentity(t *testing.T) {
	release := loadReleaseFixture(t)
	if release.SchemaVersion() != ReleaseSchemaVersion {
		t.Fatalf("schema version=%d", release.SchemaVersion())
	}
	if release.ReleaseID() != "sentinel-20260219f9f6-r1" {
		t.Fatalf("release id=%q", release.ReleaseID())
	}
	if release.ManifestSHA256() != verifiedManifestSHA256 {
		t.Fatalf("manifest hash=%q", release.ManifestSHA256())
	}
	if release.PayloadIndex5Policy() != PayloadIndex5Known || release.PayloadIndex6Policy() != PayloadIndex6Null {
		t.Fatalf("payload policies=%q/%q", release.PayloadIndex5Policy(), release.PayloadIndex6Policy())
	}
	if got := PinnedSDKHash(); got != "4f8ef8d5870894fd0101fc40ff45ea13c0f8e25c71c2ba28e5df5baf98babbb5" {
		t.Fatalf("embedded SDK pin changed: %s", got)
	}
	if err := release.ValidateEmbeddedPin(); err != nil {
		t.Fatalf("ValidateEmbeddedPin: %v", err)
	}

	d17, err := release.ResolveScriptSource(PinnedSDKURL)
	if err != nil {
		t.Fatalf("resolve d17 versioned source: %v", err)
	}
	d24, err := release.ResolveScriptSource(PinnedLoaderURL)
	if err != nil {
		t.Fatalf("resolve d24 loader source: %v", err)
	}
	if d17.Kind != ScriptSourceSDK || d24.Kind != ScriptSourceLoader {
		t.Fatalf("source kinds d17=%q d24=%q", d17.Kind, d24.Kind)
	}
	if d17.ContentIdentitySHA256 != d24.ContentIdentitySHA256 || d17.SDKURL != d24.SDKURL || d17.SDKSHA256 != d24.SDKSHA256 {
		t.Fatalf("d17/d24 did not resolve to one content identity: d17=%+v d24=%+v", d17, d24)
	}
	if d17.ReleaseID != release.ReleaseID() || d17.ManifestSHA256 != release.ManifestSHA256() || d17.FrameSV != PinnedSDKVersion || d17.PatchHookID != PinnedPatchHookID {
		t.Fatalf("resolved release identity incomplete: %+v", d17)
	}
}

func TestReleaseManifestAccessorsAreImmutableCopies(t *testing.T) {
	release := loadReleaseFixture(t)
	sources := release.ObservedScriptSources()
	sources[0] = "https://attacker.invalid/sdk.js"
	if release.ObservedScriptSources()[0] != PinnedLoaderURL {
		t.Fatal("source accessor mutated release")
	}
	loader := release.Loader()
	loader.URL = "https://attacker.invalid/loader.js"
	if release.Loader().URL != PinnedLoaderURL {
		t.Fatal("loader accessor mutated release")
	}

	raw, err := release.CanonicalJSON()
	if err != nil {
		t.Fatal(err)
	}
	var normalized map[string]any
	if err := json.Unmarshal(raw, &normalized); err != nil {
		t.Fatal(err)
	}
	if normalized["manifest_sha256"] != verifiedManifestSHA256 {
		t.Fatalf("canonical manifest identity missing: %v", normalized)
	}
}

func TestReleaseManifestTreatsSourcesAsASet(t *testing.T) {
	raw, err := os.ReadFile(releaseFixturePath(t))
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	sources := doc["observed_script_sources"].([]any)
	sources[0], sources[1] = sources[1], sources[0]
	reversed, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	release, err := ParseRelease(reversed)
	if err != nil {
		t.Fatalf("source-set order changed identity: %v", err)
	}
	if release.ManifestSHA256() != verifiedManifestSHA256 {
		t.Fatalf("manifest identity=%q", release.ManifestSHA256())
	}
	if got := release.ObservedScriptSources(); !reflect.DeepEqual(got, []string{PinnedLoaderURL, PinnedSDKURL}) {
		t.Fatalf("normalized sources=%v", got)
	}
}

func TestReleasePayloadSamplingCanSelectBothKnownSources(t *testing.T) {
	release := loadReleaseFixture(t)
	bundle, err := fingerprint.Generate(fingerprint.GenerateOptions{
		RNG:          rand.New(rand.NewPCG(150, 24)),
		ForceFamily:  fingerprint.FamilyDesktop,
		ForceBrowser: fingerprint.BrowserFirefox,
	})
	if err != nil {
		t.Fatal(err)
	}
	bound, err := release.BindBundle(bundle)
	if err != nil {
		t.Fatal(err)
	}
	env := EnvFromBundle(bound)
	if !env.BuildNull || env.BuildHash != PinnedSDKVersion {
		t.Fatalf("Firefox build policy was not separated from frame build: %+v", env)
	}
	seen := map[string]bool{}
	for seed := uint64(1); seed <= 64 && len(seen) < 2; seed++ {
		payload := CollectFingerprintData(env, "seeded-sid", rand.New(rand.NewPCG(seed, seed+1)))
		if payload[6] != nil {
			t.Fatalf("payload[6]=%v want null", payload[6])
		}
		source, ok := payload[5].(string)
		if !ok {
			t.Fatalf("payload[5] type=%T", payload[5])
		}
		seen[source] = true
	}
	if !seen[PinnedLoaderURL] || !seen[PinnedSDKURL] || len(seen) != 2 {
		t.Fatalf("seeded payload[5] sources=%v", seen)
	}
}

func TestReleaseObservationValidationFailsClosed(t *testing.T) {
	release := loadReleaseFixture(t)
	if _, err := release.ValidateLoaderObservation(PinnedLoaderURL, "sha256:"+PinnedLoaderHash, PinnedSDKURL); err != nil {
		t.Fatalf("valid loader observation: %v", err)
	}
	if _, err := release.ValidateSDKObservation(PinnedSDKURL, PinnedSDKHash(), PinnedSDKVersion, PinnedPatchHookID); err != nil {
		t.Fatalf("valid SDK observation: %v", err)
	}

	cases := []struct {
		name string
		want string
		call func() error
	}{
		{"unknown source", CodeSourceUntrusted, func() error { _, err := release.ResolveScriptSource("https://sentinel.openai.com/sentinel/unknown/sdk.js"); return err }},
		{"loader hash", CodeLoaderHashMismatch, func() error { _, err := release.ValidateLoaderObservation(PinnedLoaderURL, "00", PinnedSDKURL); return err }},
		{"loader target", CodeSourceUntrusted, func() error { _, err := release.ValidateLoaderObservation(PinnedLoaderURL, PinnedLoaderHash, "https://sentinel.openai.com/sentinel/other/sdk.js"); return err }},
		{"sdk hash", CodeReleaseSDKHashMismatch, func() error { _, err := release.ValidateSDKObservation(PinnedSDKURL, "00", PinnedSDKVersion, PinnedPatchHookID); return err }},
		{"build", CodeReleaseBuildMismatch, func() error { _, err := release.ValidateSDKObservation(PinnedSDKURL, PinnedSDKHash(), "other", PinnedPatchHookID); return err }},
		{"hook", CodeReleaseHookMismatch, func() error { _, err := release.ValidateSDKObservation(PinnedSDKURL, PinnedSDKHash(), PinnedSDKVersion, "other"); return err }},
		{"empty source", CodeSourceUntrusted, func() error { _, err := release.ResolveScriptSource(""); return err }},
		{"empty loader hash", CodeLoaderHashMismatch, func() error { _, err := release.ValidateLoaderObservation(PinnedLoaderURL, "", PinnedSDKURL); return err }},
		{"empty loader target", CodeSourceUntrusted, func() error { _, err := release.ValidateLoaderObservation(PinnedLoaderURL, PinnedLoaderHash, ""); return err }},
		{"empty sdk hash", CodeReleaseSDKHashMismatch, func() error { _, err := release.ValidateSDKObservation(PinnedSDKURL, "", PinnedSDKVersion, PinnedPatchHookID); return err }},
		{"empty build", CodeReleaseBuildMismatch, func() error { _, err := release.ValidateSDKObservation(PinnedSDKURL, PinnedSDKHash(), "", PinnedPatchHookID); return err }},
		{"empty hook", CodeReleaseHookMismatch, func() error { _, err := release.ValidateSDKObservation(PinnedSDKURL, PinnedSDKHash(), PinnedSDKVersion, ""); return err }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			assertSentinelCode(t, tc.call(), tc.want)
		})
	}
}

func TestZeroValueReleaseManifestFailsClosed(t *testing.T) {
	var zero ReleaseManifest
	cases := []struct {
		name string
		call func() error
	}{
		{"embedded pin", zero.ValidateEmbeddedPin},
		{"known source", func() error { _, err := zero.ResolveScriptSource(PinnedLoaderURL); return err }},
		{"empty source", func() error { _, err := zero.ResolveScriptSource(""); return err }},
		{"empty loader observation", func() error { _, err := zero.ValidateLoaderObservation("", "", ""); return err }},
		{"empty sdk observation", func() error { _, err := zero.ValidateSDKObservation("", "", "", ""); return err }},
		{"bind", func() error { _, err := zero.BindBundle(nil); return err }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			assertSentinelCode(t, tc.call(), CodeReleaseInvalid)
		})
	}
}

func TestLoadReleaseRejectsManifestDrift(t *testing.T) {
	raw, err := os.ReadFile(releaseFixturePath(t))
	if err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name   string
		want   string
		mutate func(map[string]any)
	}{
		{"unknown loader URL", CodeSourceUntrusted, func(d map[string]any) { d["loader"].(map[string]any)["url"] = "https://sentinel.openai.com/backend-api/sentinel/other.js" }},
		{"loader hash", CodeLoaderHashMismatch, func(d map[string]any) { d["loader"].(map[string]any)["sha256"] = repeatHex("0") }},
		{"resolve target", CodeSourceUntrusted, func(d map[string]any) { d["loader"].(map[string]any)["resolves_to"] = "https://sentinel.openai.com/sentinel/other/sdk.js" }},
		{"unknown SDK URL", CodeSourceUntrusted, func(d map[string]any) { d["sdk"].(map[string]any)["url"] = "https://sentinel.openai.com/sentinel/other/sdk.js" }},
		{"SDK hash", CodeReleaseSDKHashMismatch, func(d map[string]any) { d["sdk"].(map[string]any)["sha256"] = repeatHex("0") }},
		{"frame build", CodeReleaseBuildMismatch, func(d map[string]any) { d["frame_sv"] = "other" }},
		{"patch hook", CodeReleaseHookMismatch, func(d map[string]any) { d["sdk"].(map[string]any)["patch_hook_id"] = "other" }},
		{"unknown observed source", CodeSourceUntrusted, func(d map[string]any) { d["observed_script_sources"].([]any)[0] = "https://attacker.invalid/sdk.js" }},
		{"duplicate observed source", CodeSourceUntrusted, func(d map[string]any) { d["observed_script_sources"].([]any)[0] = PinnedSDKURL }},
		{"index 5 policy", CodeReleaseInvalid, func(d map[string]any) { d["payload_index_5_policy"] = "first_source" }},
		{"index 6 policy", CodeReleaseInvalid, func(d map[string]any) { d["payload_index_6_policy"] = "build_string" }},
		{"manifest hash", CodeManifestHashMismatch, func(d map[string]any) { d["manifest_sha256"] = "sha256:" + repeatHex("0") }},
		{"unknown field", CodeReleaseInvalid, func(d map[string]any) { d["unexpected"] = true }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var doc map[string]any
			if err := json.Unmarshal(raw, &doc); err != nil {
				t.Fatal(err)
			}
			tc.mutate(doc)
			mutated, err := json.Marshal(doc)
			if err != nil {
				t.Fatal(err)
			}
			_, err = ParseRelease(mutated)
			assertSentinelCode(t, err, tc.want)
		})
	}
}

func TestBindBundleClonesFreezesAndChangesReleaseSetIdentity(t *testing.T) {
	release := loadReleaseFixture(t)
	original, err := fingerprint.Generate(fingerprint.GenerateOptions{
		RNG:          rand.New(rand.NewPCG(17, 24)),
		ForceFamily:  fingerprint.FamilyDesktop,
		ForceBrowser: fingerprint.BrowserFirefox,
	})
	if err != nil {
		t.Fatal(err)
	}
	original.SentinelEnv.ScriptSources = []string{PinnedSDKURL}
	original.SentinelEnv.BuildHash = PinnedSDKVersion
	original.Consistency = fingerprint.Consistency{}
	if err := original.Freeze(); err != nil {
		t.Fatal(err)
	}
	before, err := json.Marshal(original)
	if err != nil {
		t.Fatal(err)
	}
	originalHash := original.Consistency.Hash

	bound, err := release.BindBundle(original)
	if err != nil {
		t.Fatal(err)
	}
	if bound == original {
		t.Fatal("BindBundle returned original pointer")
	}
	after, err := json.Marshal(original)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(before, after) {
		t.Fatal("BindBundle mutated original")
	}
	if err := bound.AssertReady(); err != nil {
		t.Fatalf("bound bundle not ready: %v", err)
	}
	if got := bound.SentinelEnv.ScriptSources; !reflect.DeepEqual(got, release.ObservedScriptSources()) {
		t.Fatalf("bound sources=%v", got)
	}
	if bound.SentinelEnv.BuildHash != release.FrameSV() {
		t.Fatalf("bound build=%q", bound.SentinelEnv.BuildHash)
	}
	if bound.Consistency.Hash == originalHash {
		t.Fatalf("release source-set change did not alter frozen identity: %s", originalHash)
	}

	boundAgain, err := release.BindBundle(original)
	if err != nil {
		t.Fatal(err)
	}
	if boundAgain.Consistency.Hash != bound.Consistency.Hash {
		t.Fatalf("same release binding is nondeterministic: %s != %s", boundAgain.Consistency.Hash, bound.Consistency.Hash)
	}
}

func assertSentinelCode(t *testing.T, err error, want string) {
	t.Helper()
	if err == nil {
		t.Fatalf("expected %s error", want)
	}
	var sentinelErr *Error
	if !errors.As(err, &sentinelErr) {
		t.Fatalf("error type=%T value=%v", err, err)
	}
	if sentinelErr.Code != want {
		t.Fatalf("error code=%q want=%q (%v)", sentinelErr.Code, want, err)
	}
}

func repeatHex(nibble string) string {
	return strings.Repeat(nibble, 64)
}
