// Package main is the email-protocol worker: ledger, V2 API, mailat real runner (or synthetic).
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/gpt-register/go-email-protocol/internal/admission"
	"github.com/gpt-register/go-email-protocol/internal/api"
	"github.com/gpt-register/go-email-protocol/internal/cryptostore"
	"github.com/gpt-register/go-email-protocol/internal/job"
	"github.com/gpt-register/go-email-protocol/internal/ledger"
	"github.com/gpt-register/go-email-protocol/internal/mailbox"
	"github.com/gpt-register/go-email-protocol/internal/sentinel"
	"github.com/gpt-register/go-email-protocol/internal/transport"
)

const version = "0.3.0-protocol"

func main() {
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "version", "-version", "--version":
			fmt.Println(version)
			return
		}
	}

	addr := flag.String("addr", "127.0.0.1:18765", "listen address (loopback recommended)")
	dbPath := flag.String("db", "email-protocol-ledger.db", "sqlite ledger path")
	keyPath := flag.String("key", "email-protocol.key", "AES key file for secret blobs")
	maxActive := flag.Int("max-active", admission.DefaultMaxActive, "max concurrent active jobs")
	graphMaxConcurrent := flag.Int("graph-max-concurrent", 96, "max concurrent Microsoft Graph HTTP requests")
	businessDBPath := flag.String("business-db", filepath.Join("..", "data", "gpt_register.db"), "business DB SQLite fallback; env.db/Postgres overrides it")
	mailatDir := flag.String("mailat-dir", defaultMailatDir(), "path to mailat/codex_register (real protocol)")
	workRoot := flag.String("work-root", "", "per-job work directory root")
	synthetic := flag.Bool("synthetic", false, "use synthetic runner instead of mailat (tests only)")
	pureGo := flag.Bool("pure-go", false, "disable mailat; use Go protocol engine (pair with -protocol-mode)")
	protocolMode := flag.String("protocol-mode", "", "when mailat disabled: synthetic|engine|live (live=real OpenAI via transport)")
	transportName := flag.String("transport", "fake", "HTTP transport factory: fake|tls|direct (tls needs -tags tlsclient; direct=SOCKS5 BridgeURL)")
	skipSDKDrift := flag.Bool("skip-sdk-drift", false, "skip live Sentinel SDK hash/build drift check (embed pin still validated)")
	failSDKNetwork := flag.Bool("sdk-drift-fail-network", false, "treat SDK drift network errors as fatal at startup")
	flag.Parse()
	mailbox.SetGraphMaxConcurrent(*graphMaxConcurrent)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// 1) embed pin + hook (hard)
	// 2) live content hash + frame build id (hard on mismatch; network soft by default)
	driftRes, driftErr := sentinel.StartupDriftCheck(ctx, sentinel.StartupDriftOptions{
		SkipNetwork:   *skipSDKDrift,
		FailOnNetwork: *failSDKNetwork,
		Timeout:       15 * time.Second,
	})
	sentinel.RecheckDriftStore(driftRes)
	if driftErr != nil {
		fmt.Fprintln(os.Stderr, "sdk_drift:", driftErr)
		os.Exit(1)
	}
	if driftRes.Kind == sentinel.DriftNetwork {
		fmt.Printf("sdk_drift=soft_network pin=%s detail=%s\n", driftRes.PinnedVersion, driftRes.Error)
	} else {
		fmt.Printf("sdk_drift=ok pin=%s hash=%s builds=%v\n",
			driftRes.PinnedVersion, shortHash(driftRes.PinnedHash), driftRes.LiveBuilds)
	}

	key, err := loadOrCreateKey(*keyPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "key:", err)
		os.Exit(1)
	}
	crypto, err := cryptostore.NewFromKey(key)
	if err != nil {
		fmt.Fprintln(os.Stderr, "crypto:", err)
		os.Exit(1)
	}
	led, err := ledger.Open(*dbPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "ledger:", err)
		os.Exit(1)
	}
	defer led.Close()

	adm := admission.New(admission.Config{MaxActive: *maxActive, MaxQueued: *maxActive * 2})
	runnerCfg := job.RunnerConfig{BusinessDBPath: *businessDBPath, SessionRemints: 2}
	mode := strings.ToLower(strings.TrimSpace(*protocolMode))
	useMailat := !*synthetic && !*pureGo && mode != "live" && mode != "engine"
	runtimeInfo := api.RuntimeInfo{
		Transport:          strings.ToLower(strings.TrimSpace(*transportName)),
		GraphMaxConcurrent: *graphMaxConcurrent,
	}
	if useMailat {
		root := *workRoot
		if root == "" {
			root = filepath.Join(".", "data", "go-email-protocol-jobs")
		}
		runnerCfg.Mailat = job.MailatConfig{
			Enabled:   true,
			MailatDir: *mailatDir,
			WorkRoot:  root,
		}
		runtimeInfo.Runner = "mailat"
		fmt.Printf("runner=mailat dir=%s work=%s\n", *mailatDir, root)
	} else {
		if mode == "" {
			if *synthetic {
				mode = "synthetic"
			} else if *pureGo {
				mode = "live"
			} else {
				mode = "synthetic"
			}
		}
		runnerCfg.ProtocolMode = mode
		runtimeInfo.Runner = "protocol"
		runtimeInfo.ProtocolMode = mode
		fmt.Printf("runner=protocol mode=%s\n", mode)
	}
	tf, err := transport.NewFactory(*transportName)
	if err != nil {
		fmt.Fprintln(os.Stderr, "transport:", err)
		os.Exit(1)
	}
	fmt.Printf("transport=%s\n", *transportName)
	mgr := job.NewManager(led, adm, crypto, tf, runnerCfg)
	defer mgr.Close()

	if err := mgr.RecoverNonTerminal(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "recover:", err)
		os.Exit(1)
	}

	ver := version
	if useMailat {
		ver = "0.3.0-mailat"
	}
	srv := api.New(mgr, ver, runtimeInfo)
	fmt.Printf("email-protocol-worker %s listening on %s (db=%s) runner=%s mode=%s transport=%s max_active=%d graph_max_concurrent=%d\n",
		ver, *addr, *dbPath, runtimeInfo.Runner, runtimeInfo.ProtocolMode, runtimeInfo.Transport, *maxActive, *graphMaxConcurrent)
	if err := srv.ListenAndServe(ctx, *addr); err != nil {
		fmt.Fprintln(os.Stderr, "serve:", err)
		os.Exit(1)
	}
}

func shortHash(h string) string {
	if len(h) <= 12 {
		return h
	}
	return h[:12] + "…"
}

func defaultMailatDir() string {
	candidates := []string{
		`E:\project\mailat\mailat\codex_register`,
		filepath.Join("..", "..", "mailat", "mailat", "codex_register"),
	}
	for _, c := range candidates {
		if st, err := os.Stat(filepath.Join(c, "src", "index.ts")); err == nil && !st.IsDir() {
			return c
		}
	}
	return candidates[0]
}

func loadOrCreateKey(path string) ([]byte, error) {
	b, err := os.ReadFile(path)
	if err == nil && len(b) >= 16 {
		return b, nil
	}
	_, raw, err := cryptostore.NewRandomKey()
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		return nil, err
	}
	return raw, nil
}
