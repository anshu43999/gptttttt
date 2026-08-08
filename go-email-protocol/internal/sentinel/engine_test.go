package sentinel

import (
	"context"
	"strings"
	"testing"
	"time"
)

func TestParseAndSolve(t *testing.T) {
	raw := []byte(`{"difficulty":1,"token":"abc","flow":"authorize_continue"}`)
	e := &Engine{Cfg: Config{MaxAttempts: 100000, Timeout: 5 * time.Second, DeviceID: "dev1"}}
	res, err := e.Run(context.Background(), raw)
	if err != nil {
		t.Fatal(err)
	}
	if res.HeaderValue == "" || res.P == "" {
		t.Fatalf("%+v", res)
	}
	if !strings.HasPrefix(res.P, "gAAAAAB") {
		t.Fatalf("p prefix %s", res.P)
	}
	if !strings.HasPrefix(res.RequirementsToken, "gAAAAAC") {
		t.Fatalf("c prefix %s", res.RequirementsToken)
	}
	if !strings.Contains(res.HeaderValue, "authorize_continue") {
		t.Fatal(res.HeaderValue)
	}
}

func TestSDKPinReject(t *testing.T) {
	raw := []byte(`{"difficulty":1,"token":"t","sdk_hash":"deadbeef"}`)
	e := &Engine{Cfg: Config{
		MaxAttempts:      1000,
		AllowedSDKHashes: map[string]bool{"good": true},
	}}
	_, err := e.Run(context.Background(), raw)
	if err == nil {
		t.Fatal("expected pin fail")
	}
	if se, ok := err.(*Error); !ok || se.Code != "protocol_incompatible" {
		t.Fatalf("%v", err)
	}
}

func TestPowExhausted(t *testing.T) {
	// difficulty string of many zeros is hard for 8-char hash
	raw := []byte(`{"difficulty":"00000000","token":"x"}`)
	e := &Engine{Cfg: Config{MaxAttempts: 5}}
	_, err := e.Run(context.Background(), raw)
	if err == nil {
		t.Fatal("expected exhaust")
	}
}
