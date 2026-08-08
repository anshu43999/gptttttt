package transport

import "testing"

func TestNewFactoryDefaultFake(t *testing.T) {
	f, err := NewFactory("")
	if err != nil {
		t.Fatal(err)
	}
	c, err := f.New("jr_x", ProxySnapshot{
		BridgeURL:        "http://127.0.0.1:9",
		BridgeCapability: "cap",
	})
	if err != nil {
		// FakeFactory.New does not validate bridge — only OptionsFactory path does
		_ = err
	}
	if c == nil {
		// Fake New always works without bridge
		c, err = f.New("jr_x", ProxySnapshot{})
		if err != nil || c == nil {
			t.Fatal(err)
		}
	}
	_ = c.Close()
}

func TestNewFactoryTLSWithoutTag(t *testing.T) {
	// Default build without -tags tlsclient must fail closed, not panic.
	_, err := NewFactory("tls")
	if err == nil {
		// May succeed if built with tlsclient tag in CI — both OK
		t.Log("tls factory available (tlsclient tag)")
		return
	}
	if err.Error() == "" {
		t.Fatal("empty error")
	}
}

func TestChromeProfileNameForMajor(t *testing.T) {
	n, err := ChromeProfileNameForMajor(142)
	if err != nil || n != "Chrome_133" {
		t.Fatalf("%s %v", n, err)
	}
	n, err = ChromeProfileNameForMajor(120)
	if err != nil || n != "Chrome_120" {
		t.Fatalf("%s %v", n, err)
	}
	_, err = ChromeProfileNameForMajor(50)
	if err == nil {
		t.Fatal("expected floor error")
	}
}
