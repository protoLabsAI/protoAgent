// Stand-in for mock-server.mjs in mock-server-resilience.spec.ts: a "server" that dies, so the
// supervisor's exit report can be asserted without killing the mock the other specs share.
console.error("[fixture] Error: the last thing the server said");
if (process.argv[2] === "signal") process.kill(process.pid, "SIGKILL");
else process.exit(3);
