package replay

import (
	"net/http"
	"net/url"
	"reflect"
	"testing"
)

func TestSymbolicJarUsesRealCookieScope(t *testing.T) {
	jar, err := NewSymbolicJar()
	if err != nil {
		t.Fatal(err)
	}
	value, err := jar.Value("auth_session")
	if err != nil {
		t.Fatal(err)
	}
	authURL, _ := url.Parse("https://auth.openai.com/email-verification")
	jar.SetCookies(authURL, []*http.Cookie{{
		Name:     "auth-session",
		Value:    value,
		Path:     "/email-verification",
		Secure:   true,
		HttpOnly: true,
	}})

	if got := jar.CookieNames(authURL); !reflect.DeepEqual(got, []string{"auth-session"}) {
		t.Fatalf("auth cookie names = %v", got)
	}
	outsidePath, _ := url.Parse("https://auth.openai.com/about-you")
	if got := jar.CookieNames(outsidePath); len(got) != 0 {
		t.Fatalf("path-scoped cookie leaked: %v", got)
	}
	otherHost, _ := url.Parse("https://chatgpt.com/email-verification")
	if got := jar.CookieNames(otherHost); len(got) != 0 {
		t.Fatalf("host-scoped cookie leaked: %v", got)
	}
	if !jar.Matches("auth_session", value) {
		t.Fatal("slot value no longer matches")
	}
}

func TestSymbolicValuesAreDeterministicAndRequireSlots(t *testing.T) {
	jar, err := NewSymbolicJar()
	if err != nil {
		t.Fatal(err)
	}
	first, err := jar.Value("csrf")
	if err != nil {
		t.Fatal(err)
	}
	second, err := jar.Value("csrf")
	if err != nil {
		t.Fatal(err)
	}
	if first != second {
		t.Fatalf("symbol changed: %q != %q", first, second)
	}
	if _, err := jar.Value(""); err == nil {
		t.Fatal("empty symbolic slot accepted")
	}
}
