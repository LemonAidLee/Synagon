// Deliberately almost empty.
//
// A preload script is the one place a desktop shell can hand a page powers a browser tab
// does not have - file system access, process spawning, native dialogs. This one hands it
// none of that, on purpose: the cockpit was proven in a browser tab (Roadmap Phases 8-10),
// and everything it needs it gets from the daemon over loopback. Giving the page privileged
// bridges here would create a second way to act that no daemon route governs, which is
// exactly the shape invariant 12 exists to prevent.
//
// What it does expose is one read-only fact: that the page is running inside the shell rather
// than a browser tab, so a future version can, say, offer "Open Folder" in an empty state.

const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("orchestratorShell", {
  present: true,
  platform: process.platform,
});
