// Features that are built and working, but deliberately not shown.
//
// Hiding beats deleting here: the code stays covered by its tests and stays
// honest about existing, and turning one of these back on is a one-word edit
// rather than a revert of a deletion nobody remembers making.

// The classroom join page (/join): mDNS discovery + pairing, so phones join
// over WiFi with no USB cable.
//
// Hidden 2026-08-03 after testing it on the school network, which puts
// clients in an isolated environment -- mDNS discovery finds nothing there,
// and it cannot: client isolation drops the multicast the scan depends on.
// The page is not broken; the network it was written for is not the network
// it will be used on. USB, or `adb connect <ip>:<port>` from the run list
// when the address is known, both still work.
export const CLASSROOM_JOIN_ENABLED = false

// The Task Queue page (/queue): Celery's own view of itself.
//
// Hidden 2026-08-04. It is the one page whose numbers cannot be trusted at a
// glance -- a `--pool=solo` worker cannot answer an inspect() broadcast while
// it is executing a task, so "active" and "reserved" read as zero for the
// whole duration of every run. The trustworthy parts of it (what is running,
// how many are waiting) are sourced from the database and shown elsewhere;
// what is left is a page that contradicts the run list.
export const TASK_QUEUE_ENABLED = false
