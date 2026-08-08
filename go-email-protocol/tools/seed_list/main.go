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
	rows, err := c.Query(context.Background(), `
		SELECT id, resource_key, status, left(payload_json, 280)
		FROM resource_pool
		WHERE provider='proxy_seed'
		ORDER BY id`)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer rows.Close()
	n := 0
	for rows.Next() {
		var id int64
		var key, st, pay string
		if err := rows.Scan(&id, &key, &st, &pay); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		fmt.Printf("%d %s %s %s\n", id, key, st, pay)
		n++
	}
	fmt.Println("count", n)
}
