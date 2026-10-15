// Release signing keys this launcher was built with: the key Desktop Companion carries in
// runtime.rs and the command-line client in command_line.py. A runtime is used only when its
// manifest is signed by one of them; nothing received from the network can add a key.
export const TRUSTED_KEYS = Object.freeze({
  "home-server-2026-09": "0j9MtkDqgSW5Z+tjC6aSv/5X2qGDZA9nYoJyqJbhZP8=",
});
