// Command fixture-recorder validates an on-disk fixture catalogue and can
// record an offline observation JSON into a redacted Fixture via
// fixture.RecordFromObservation. It never opens the network.
//
// Usage:
//
//	fixture-recorder validate [testdata-root]
//	fixture-recorder record <observation.json>
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/gpt-register/go-email-protocol/internal/fixture"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintf(os.Stderr, "usage: %s validate [testdata-root] | record <observation.json>\n", os.Args[0])
		os.Exit(2)
	}
	switch os.Args[1] {
	case "validate":
		root := "testdata"
		if len(os.Args) > 2 {
			root = os.Args[2]
		}
		cat, err := fixture.LoadCatalogue(root)
		if err != nil {
			fmt.Fprintf(os.Stderr, "load: %v\n", err)
			os.Exit(1)
		}
		if err := cat.ValidateCompleteness(); err != nil {
			fmt.Fprintf(os.Stderr, "complete: %v\n", err)
			os.Exit(1)
		}
		fmt.Printf("ok: %d fixtures (specified=%d capture_required=%d)\n",
			len(cat.Fixtures), len(cat.Specified()), len(cat.CaptureRequired()))
	case "record":
		if len(os.Args) < 3 {
			fmt.Fprintf(os.Stderr, "usage: %s record <observation.json>\n", os.Args[0])
			os.Exit(2)
		}
		raw, err := os.ReadFile(os.Args[2])
		if err != nil {
			fmt.Fprintf(os.Stderr, "read: %v\n", err)
			os.Exit(1)
		}
		var draft fixture.Fixture
		if err := json.Unmarshal(raw, &draft); err != nil {
			fmt.Fprintf(os.Stderr, "json: %v\n", err)
			os.Exit(1)
		}
		out, err := fixture.RecordFromObservation(fixture.Observation{Fixture: draft, Raw: raw})
		if err != nil {
			fmt.Fprintf(os.Stderr, "record: %v\n", err)
			os.Exit(1)
		}
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		if err := enc.Encode(out); err != nil {
			fmt.Fprintf(os.Stderr, "encode: %v\n", err)
			os.Exit(1)
		}
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q\n", os.Args[1])
		os.Exit(2)
	}
}
