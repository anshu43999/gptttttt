package main

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"time"

	"golang.org/x/net/proxy"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: proxy_dial_check <socks5-url>")
		os.Exit(2)
	}
	raw := os.Args[1]
	u, err := url.Parse(raw)
	if err != nil {
		panic(err)
	}
	var auth *proxy.Auth
	if u.User != nil {
		pass, _ := u.User.Password()
		auth = &proxy.Auth{User: u.User.Username(), Password: pass}
	}
	d, err := proxy.SOCKS5("tcp", u.Host, auth, &net.Dialer{Timeout: 15 * time.Second})
	if err != nil {
		panic(err)
	}
	tr := &http.Transport{
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			return d.Dial(network, addr)
		},
	}
	c := &http.Client{Transport: tr, Timeout: 25 * time.Second}
	resp, err := c.Get("https://api.ipify.org?format=json")
	if err != nil {
		fmt.Println("DIAL_ERR", err)
		os.Exit(2)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 256))
	fmt.Println("OK", resp.StatusCode, string(b))
}
