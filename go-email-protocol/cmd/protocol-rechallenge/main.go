package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/gpt-register/go-email-protocol/internal/rechallenge"
)

type cliOptions struct {
	input            string
	second           string
	out              string
	manifestOut      string
	contractOut      string
	captureID        string
	role             rechallenge.CaptureRole
	redactionPolicy  string
	sha256           string
	sentinelRelease  string
	transportProfile string
	policy           string
}

func main() {
	if err := run(context.Background(), os.Args[1:]); err != nil {
		var contractErr *rechallenge.ContractError
		if errors.As(err, &contractErr) {
			raw, _ := json.Marshal(contractErr)
			fmt.Fprintln(os.Stderr, string(raw))
		} else {
			fmt.Fprintln(os.Stderr, err)
		}
		os.Exit(1)
	}
}

func run(ctx context.Context, args []string) error {
	if len(args) == 0 {
		return usageError()
	}
	command := args[0]
	options, err := parseCLIOptions(args[1:])
	if err != nil {
		return err
	}
	switch command {
	case "ingest":
		if options.input == "" {
			return usageError()
		}
		capture, err := rechallenge.IngestHAR(ctx, options.input, rechallenge.IngestOptions{
			CaptureID:         options.captureID,
			ExpectedRole:      options.role,
			ExpectedSHA256:    options.sha256,
			RedactionPolicyID: options.redactionPolicy,
		})
		if err != nil {
			return err
		}
		if options.out != "" {
			return rechallenge.SaveCaptureManifest(options.out, &capture.Manifest)
		}
		return writeJSON(os.Stdout, &capture.Manifest)
	case "normalize":
		if options.input == "" {
			return usageError()
		}
		normalized, err := rechallenge.NormalizeHAR(ctx, options.input, rechallenge.NormalizeOptions{
			CaptureID:          options.captureID,
			ExpectedRole:       options.role,
			ExpectedSHA256:     options.sha256,
			RedactionPolicyID:  options.redactionPolicy,
			SentinelReleaseID:  options.sentinelRelease,
			TransportProfileID: options.transportProfile,
			PolicyID:           options.policy,
		})
		if err != nil {
			return err
		}
		manifestOut := options.manifestOut
		contractOut := options.contractOut
		if options.out != "" && contractOut == "" {
			contractOut = options.out
		}
		if manifestOut != "" {
			if err := rechallenge.SaveCaptureManifest(manifestOut, normalized.Manifest); err != nil {
				return err
			}
		}
		if contractOut != "" {
			return rechallenge.SaveContract(contractOut, normalized.Contract)
		}
		return writeJSON(os.Stdout, normalized)
	case "diff":
		if options.input == "" || options.second == "" {
			return usageError()
		}
		approved, err := rechallenge.LoadContract(options.input)
		if err != nil {
			return err
		}
		candidate, err := rechallenge.LoadContract(options.second)
		if err != nil {
			return err
		}
		report, err := rechallenge.DiffContracts(approved, candidate)
		if err != nil {
			return err
		}
		if options.out != "" {
			raw, err := json.MarshalIndent(report, "", "  ")
			if err != nil {
				return err
			}
			if err := rechallenge.ValidateRedactedJSON(options.out, raw); err != nil {
				return err
			}
			if err := os.WriteFile(options.out, append(raw, '\n'), 0o600); err != nil {
				return err
			}
		} else if err := writeJSON(os.Stdout, report); err != nil {
			return err
		}
		if report.HasBlocking() {
			return fmt.Errorf("rechallenge: semantic diff blocked with %d field differences", len(report.Blocking))
		}
		return nil
	default:
		return usageError()
	}
}

func parseCLIOptions(args []string) (cliOptions, error) {
	var options cliOptions
	for index := 0; index < len(args); index++ {
		argument := args[index]
		if !strings.HasPrefix(argument, "--") {
			if options.input == "" {
				options.input = argument
			} else if options.second == "" {
				options.second = argument
			} else {
				return options, fmt.Errorf("protocol-rechallenge: unexpected argument %q", argument)
			}
			continue
		}
		key := strings.TrimPrefix(argument, "--")
		var value string
		if equals := strings.IndexByte(key, '='); equals >= 0 {
			value = key[equals+1:]
			key = key[:equals]
		} else {
			index++
			if index >= len(args) {
				return options, fmt.Errorf("protocol-rechallenge: --%s requires a value", key)
			}
			value = args[index]
		}
		switch key {
		case "out":
			options.out = value
		case "manifest-out":
			options.manifestOut = value
		case "contract-out":
			options.contractOut = value
		case "capture-id":
			options.captureID = value
		case "role":
			options.role = rechallenge.CaptureRole(value)
		case "redaction-policy":
			options.redactionPolicy = value
		case "sha256":
			options.sha256 = value
		case "sentinel-release":
			options.sentinelRelease = value
		case "transport-profile":
			options.transportProfile = value
		case "policy":
			options.policy = value
		default:
			return options, fmt.Errorf("protocol-rechallenge: unknown option --%s", key)
		}
	}
	return options, nil
}

func writeJSON(file *os.File, value any) error {
	raw, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	if err := rechallenge.ValidateRedactedJSON("protocol-rechallenge-output", raw); err != nil {
		return err
	}
	_, err = file.Write(append(raw, '\n'))
	return err
}

func usageError() error {
	return errors.New("usage: protocol-rechallenge ingest <capture.har> [--capture-id ID --role registration --sha256 HEX --redaction-policy registration-v1 --out manifest.json] | normalize <capture.har> [--manifest-out manifest.json --contract-out contract.json] | diff <approved-contract> <candidate-contract> [--out report.json]")
}
