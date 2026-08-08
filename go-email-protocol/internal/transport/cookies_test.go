package transport

import (
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"testing"
)

func TestExportImportHTTPJar(t *testing.T) {
	j, err := cookiejar.New(nil)
	if err != nil {
		t.Fatal(err)
	}
	u, _ := url.Parse("https://auth.openai.com/")
	j.SetCookies(u, []*http.Cookie{{Name: "oai-did", Value: "dev-1", Path: "/", Domain: "auth.openai.com"}})
	raw, err := ExportHTTPJar(j)
	if err != nil {
		t.Fatal(err)
	}
	j2, _ := cookiejar.New(nil)
	if err := ImportHTTPJar(j2, raw); err != nil {
		t.Fatal(err)
	}
	cs := j2.Cookies(u)
	found := false
	for _, c := range cs {
		if c.Name == "oai-did" && c.Value == "dev-1" {
			found = true
		}
	}
	if !found {
		t.Fatalf("cookie missing: %+v raw=%s", cs, raw)
	}
}
