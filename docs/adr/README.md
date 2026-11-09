# Decision records

- [0001-hub-not-peer-to-peer.md](0001-hub-not-peer-to-peer.md):
  multi-host claims go through a hub, not peer-to-peer acks
  (accepted; design in `../08-hub-multihost.md`).
- [0002-remove-opencode.md](0002-remove-opencode.md):
  opencode support removed; re-adding is a new adapter ticket
  (accepted; closes the opencode 2.x cutover).
- [0003-doctor-git-seam-stays-separate.md](0003-doctor-git-seam-stays-separate.md):
  the doctor keeps its own git runner beside `syncli._git`
  (accepted; kept separate in ticket 712, recorded for ticket 709).

To add one, write a numbered markdown file named `NNNN-title.md` with status, context, decision and consequences.
