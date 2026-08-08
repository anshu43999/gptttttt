
package main
import (
  "fmt"
  "io"
  "net/http"
  "net/url"
  "os"
  "time"
  "golang.org/x/net/proxy"
  proxypool "github.com/gpt-register/go-email-protocol/internal/proxy"
)
func main() {
  db := os.Getenv("GPT_REGISTER_DB_PATH")
  if db == "" { db = "../data/gpt_register.db" }
  styles := []string{"bestgo","1024"}
  regions := []string{"JP","US","DE"}
  targets := []string{
    "https://api.ipify.org",
    "https://chatgpt.com/api/auth/providers",
  }
  for _, st := range styles {
    for _, rg := range regions {
      sess, err := proxypool.MintSeedSession(db, "probe_"+st+"_"+rg, []string{st}, rg, 10)
      if err != nil { fmt.Printf("%s %s mint_err=%v\n", st, rg, err); continue }
      u, _ := url.Parse(sess.URL)
      auth := &proxy.Auth{}
      if u.User != nil {
        auth.User = u.User.Username()
        auth.Password, _ = u.User.Password()
      }
      d, err := proxy.SOCKS5("tcp", u.Host, auth, proxy.Direct)
      if err != nil { fmt.Printf("%s %s dialer_err=%v\n", st, rg, err); continue }
      tr := &http.Transport{Dial: d.Dial}
      client := &http.Client{Transport: tr, Timeout: 20*time.Second}
      for _, t := range targets {
        t0 := time.Now()
        resp, err := client.Get(t)
        if err != nil {
          fmt.Printf("%s %s %s ERR %v\n", st, rg, t, err)
          continue
        }
        b, _ := io.ReadAll(io.LimitReader(resp.Body, 120))
        resp.Body.Close()
        fmt.Printf("%s %s %s OK status=%d ms=%d body=%q\n", st, rg, t, resp.StatusCode, time.Since(t0).Milliseconds(), string(b))
      }
    }
  }
}
