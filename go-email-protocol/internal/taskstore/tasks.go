// Package taskstore writes Go-managed registration task state into the business
// database.  The dashboard remains a view/control plane; it does not execute
// protocol jobs or lease registration resources.
package taskstore

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/gpt-register/go-email-protocol/internal/store"
)

const RegistrationTaskType = "email-protocol-register-token"

type Result struct {
	BatchID   string `json:"go_batch_id"`
	Managed   bool   `json:"go_managed"`
	JobID     string `json:"go_job_id,omitempty"`
	AccountID string `json:"account_id,omitempty"`
	Email     string `json:"email,omitempty"`
}

// CreateRegistrationTasks creates dashboard-visible task rows already owned by
// the Go daemon. They start as running so TasksService never claims them for a
// Python inline worker.
func CreateRegistrationTasks(dbPath, batchID string, count int) ([]string, error) {
	if strings.TrimSpace(batchID) == "" {
		return nil, fmt.Errorf("taskstore: batch_id required")
	}
	if count < 1 {
		return nil, fmt.Errorf("taskstore: count must be positive")
	}
	db, backend, err := store.OpenPath(dbPath)
	if err != nil {
		return nil, err
	}
	defer db.Close()

	now := time.Now().UTC().Format(time.RFC3339Nano)
	ids := make([]string, 0, count)
	tx, err := db.Begin()
	if err != nil {
		return nil, err
	}
	defer func() { _ = tx.Rollback() }()

	insertTask := store.Rebind(backend, `
		INSERT INTO tasks(
			id, task_type, status, account_id_ref, params_json, result_json,
			created_at, started_at, finished_at, updated_at, error, retryable,
			command_json, log_file
		) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
	`)
	insertEvent := store.Rebind(backend, `
		INSERT INTO task_events(task_id, timestamp, level, event_type, message, data_json)
		VALUES(?,?,?,?,?,?)
	`)
	for range count {
		id := "goep_" + uuid.NewString()
		params, _ := json.Marshal(map[string]any{"batch_id": batchID, "go_batch_id": batchID, "go_managed": true})
		result, _ := json.Marshal(Result{BatchID: batchID, Managed: true})
		// retryable is INTEGER on both SQLite and Postgres (not bool).
		if _, err := tx.Exec(insertTask, id, RegistrationTaskType, "running", nil, string(params), string(result), now, now, "", now, "", 0, "[]", ""); err != nil {
			return nil, err
		}
		event, _ := json.Marshal(map[string]any{"batch_id": batchID, "go_managed": true})
		if _, err := tx.Exec(insertEvent, id, now, "info", "go_batch_started", "Go 注册调度已接管任务", string(event)); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return ids, nil
}

func StartJob(dbPath, taskID, batchID, jobID string) error {
	return update(dbPath, taskID, "running", "", false, "", Result{BatchID: batchID, Managed: true, JobID: jobID})
}

func Succeed(dbPath, taskID, batchID, jobID, email, accountID string) error {
	return update(dbPath, taskID, "succeeded", "", false, time.Now().UTC().Format(time.RFC3339Nano), Result{
		BatchID: batchID, Managed: true, JobID: jobID, Email: email, AccountID: accountID,
	})
}

func Fail(dbPath, taskID, batchID, jobID, message string, retryable bool) error {
	return update(dbPath, taskID, "failed", safeMessage(message), retryable, time.Now().UTC().Format(time.RFC3339Nano), Result{
		BatchID: batchID, Managed: true, JobID: jobID,
	})
}

func Cancel(dbPath, taskID, batchID, jobID string) error {
	return update(dbPath, taskID, "cancelled", "Go 批调度已取消", true, time.Now().UTC().Format(time.RFC3339Nano), Result{
		BatchID: batchID, Managed: true, JobID: jobID,
	})
}

func update(dbPath, taskID, status, message string, retryable bool, finishedAt string, result Result) error {
	db, backend, err := store.OpenPath(dbPath)
	if err != nil {
		return err
	}
	defer db.Close()
	now := time.Now().UTC().Format(time.RFC3339Nano)
	data, _ := json.Marshal(result)
	retryFlag := 0
	if retryable {
		retryFlag = 1
	}
	q := store.Rebind(backend, `
		UPDATE tasks
		SET status=?, result_json=?, updated_at=?, finished_at=?, error=?, retryable=?
		WHERE id=?
	`)
	res, err := db.Exec(q, status, string(data), now, finishedAt, safeMessage(message), retryFlag, taskID)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n != 1 {
		return fmt.Errorf("taskstore: task %s not found", taskID)
	}
	event, _ := json.Marshal(map[string]any{"go_managed": true, "go_job_id": result.JobID, "batch_id": result.BatchID})
	qEvent := store.Rebind(backend, `
		INSERT INTO task_events(task_id, timestamp, level, event_type, message, data_json)
		VALUES(?,?,?,?,?,?)
	`)
	level, eventType := "info", status
	if status == "failed" {
		level = "error"
	}
	_, err = db.Exec(qEvent, taskID, now, level, "go_"+eventType, eventMessage(status), string(event))
	return err
}

func eventMessage(status string) string {
	switch status {
	case "succeeded":
		return "Go 注册任务完成"
	case "cancelled":
		return "Go 注册任务已取消"
	case "failed":
		return "Go 注册任务失败"
	default:
		return "Go 注册任务状态已更新"
	}
}

func safeMessage(value string) string {
	value = strings.ReplaceAll(strings.TrimSpace(value), "\n", " ")
	if len(value) > 300 {
		return value[:300]
	}
	return value
}
