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
	tag, err := c.Exec(context.Background(), `UPDATE resource_pool SET status='available' WHERE id=31858 AND provider='proxy_seed'`)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Println("enabled 1024 seed rows", tag.RowsAffected())
	rows, err := c.Query(context.Background(), `SELECT id, status, left(payload_json,140) FROM resource_pool WHERE provider='proxy_seed' AND status='available' ORDER BY id`)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer rows.Close()
	for rows.Next() {
		var id int64
		var st, pay string
		_ = rows.Scan(&id, &st, &pay)
		fmt.Println(id, st, pay)
	}
}
