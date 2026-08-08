package sentinel

import (
	"context"
	"fmt"
	"testing"
	"time"
)

func TestDebugLiveVMSteps(t *testing.T) {
	reqP, dx, capturedT, _, _, _, _ := loadCase001(t)
	prog, err := DecodeTurnstileProgram(dx, reqP)
	if err != nil {
		t.Fatal(err)
	}
	env := Env{
		UserAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
		Language:  "pt-BR", Languages: []string{"pt-BR", "pt", "en-US", "en"},
		ScreenWidth: 1280, ScreenHeight: 720, HardwareConcurrency: 12,
		JSHeapSizeLimit: 0,
		BuildHash:       PinnedSDKVersion,
		ScriptSources:   []string{PinnedSDKURL},
		TimeOrigin:      1784319880356,
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	for _, preset16 := range []string{"unset", "reqP", "empty"} {
		vm := NewTurnstileVM(env)
		switch preset16 {
		case "reqP":
			vm.state[16] = reqP
		case "empty":
			vm.state[16] = ""
		}
		out, err := vm.Run(ctx, append([][]any(nil), copyProg(prog)...))
		t.Logf("preset16=%s out_len=%d raw=%q raw_len=%d err=%v instr=%d settled=%v",
			preset16, len(out), vm.resultRaw, len(vm.resultRaw), err, vm.instructionCount, vm.settled)
		if err == nil && out == capturedT {
			t.Logf("EXACT match with preset16=%s", preset16)
		}
		if err == nil && len(out) > 100 {
			t.Logf("long t with preset16=%s prefix=%q", preset16, trimPrefix(out, 40))
		}
		if err != nil {
			t.Logf("  err=%v trace_tail=%v", err, vm.trace)
		}
		// dump slot 16 after run
		t.Logf("  post slot16 type=%T preview=%s", vm.state[16], previewVal(vm.state[16]))
	}
}

func copyProg(p [][]any) [][]any {
	out := make([][]any, len(p))
	for i, row := range p {
		out[i] = append([]any(nil), row...)
	}
	return out
}

func previewVal(v any) string {
	switch x := v.(type) {
	case string:
		if len(x) > 80 {
			return fmt.Sprintf("%q...", x[:80])
		}
		return fmt.Sprintf("%q", x)
	case func(args ...any) (any, error):
		return "<fn>"
	case *AnyStub:
		return x.String()
	default:
		s := fmt.Sprint(v)
		if len(s) > 80 {
			return s[:80]
		}
		return s
	}
}
