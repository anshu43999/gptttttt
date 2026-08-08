package main

import (
	"context"
	"fmt"
	"os"

	"github.com/jackc/pgx/v5"
)

func main() {
	url := os.Getenv("GPT_REGISTER_DATABASE_URL")
	if url == "" {
		url = os.Getenv("DATABASE_URL")
	}
	c, err := pgx.Connect(context.Background(), url)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer c.Close(context.Background())
	var n int
	_ = c.QueryRow(context.Background(), `SELECT count(*) FROM resource_pool WHERE provider='outlook_token' AND status='available'`).Scan(&n)
	fmt.Println("outlook_token_available", n)
	_ = c.QueryRow(context.Background(), `SELECT count(*) FROM resource_pool WHERE provider='proxy_seed' AND status='available'`).Scan(&n)
	fmt.Println("proxy_seed_available", n)
}
