"""Capabilities. A server 'provides' what its modules do; S4 provides everything.
CHAIN says which servers (in order) can serve a capability - used for server-to-server calls with fallback."""
CAPS = set()
CHAIN = {
    "wallet":  ["s1", "s4"],     # balance / users / matches (DB1)
    "history": ["s2", "s4"],     # history (DB2)
    "chat":    ["s3", "s4"],     # chat + game (DB3)
    "fraud":   ["s5", "s4"],     # fraud / device checks (DB3)
}


def provide(*caps):
    CAPS.update(caps)


def local(cap):
    return cap in CAPS
