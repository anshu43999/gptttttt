package main

import (
	"fmt"
	"os"
	"strings"

	proxypool "github.com/gpt-register/go-email-protocol/internal/proxy"
)

func main() {
	styles := []string{"bestgo", "1024"}
	if len(os.Args) > 1 {
		styles = strings.Split(os.Args[1], ",")
	}
	region := "JP"
	if len(os.Args) > 2 {
		region = os.Args[2]
	}
	s, err := proxypool.MintSeedSession("", "canary_n1", styles, region, 15)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	// Machine-readable for shell.
	fmt.Printf("STYLE=%s\nREGION=%s\nID=%d\nURL=%s\n", s.Style, s.Region, s.ResourceID, s.URL)
}
