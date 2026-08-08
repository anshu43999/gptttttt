package transport

import "testing"

func TestValidateBridgeProxy(t *testing.T) {
	err := ValidateBridgeProxy(ProxySnapshot{
		BridgeURL:        "http://127.0.0.1:18766",
		BridgeCapability: "secret",
	})
	if err != nil {
		t.Fatal(err)
	}
	err = ValidateBridgeProxy(ProxySnapshot{
		BridgeURL:        "http://8.8.8.8:18766",
		BridgeCapability: "secret",
	})
	if err == nil {
		t.Fatal("expected non-loopback reject")
	}
	err = ValidateBridgeProxy(ProxySnapshot{
		BridgeURL: "http://127.0.0.1:18766",
	})
	if err == nil {
		t.Fatal("expected missing capability")
	}
	err = ValidateBridgeProxy(ProxySnapshot{
		BridgeURL:        "socks5://user:pass@proxy.example:7878",
		BridgeCapability: "direct",
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestFakeFactoryOptions(t *testing.T) {
	var f FakeFactory
	c, err := f.NewWithOptions(ClientOptions{
		JobID: "jr_1",
		Proxy: ProxySnapshot{
			BridgeURL:        "http://127.0.0.1:9",
			BridgeCapability: "cap",
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if c.JobID() != "jr_1" {
		t.Fatal(c.JobID())
	}
	_ = c.Close()
}

func TestMajorFromProfileID(t *testing.T) {
	n, ok := MajorFromProfileID("chrome-142-win-h2-v1")
	if !ok || n != 142 {
		t.Fatalf("%v %v", n, ok)
	}
	n, ok = MajorFromProfileID("firefox-150-win-h2-v1")
	if !ok || n != 150 {
		t.Fatalf("firefox %v %v", n, ok)
	}
	if BrowserFromProfileID("firefox-150-win-h2-v1") != "firefox" {
		t.Fatal("browser firefox")
	}
}
