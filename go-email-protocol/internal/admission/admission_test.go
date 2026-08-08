package admission

import "testing"

func TestSnapshotReportsAggregateCounts(t *testing.T) {
	controller := New(Config{MaxActive: 3, MaxQueued: 2})
	if err := controller.TryAdmit(Seat{JobID: "job-1"}); err != nil {
		t.Fatal(err)
	}
	if err := controller.TryAdmit(Seat{JobID: "job-2"}); err != nil {
		t.Fatal(err)
	}
	if err := controller.TryQueue(); err != nil {
		t.Fatal(err)
	}

	if got, want := controller.Snapshot(), (Snapshot{MaxActive: 3, ActiveCount: 2, QueuedCount: 1}); got != want {
		t.Fatalf("snapshot=%+v want %+v", got, want)
	}
}
