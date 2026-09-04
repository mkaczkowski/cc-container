# When guests have no internet, and what to do about it

On most Macs, `apple/container` guests get working outbound network and you can
leave `CC_PROXY=off`. On some machines they do not, and the failure is confusing
enough to be worth documenting.

## The symptom

The guest has a correct vmnet NAT config and no external TCP egress at all:

| Probe from inside the guest | Result |
|---|---|
| Interface / route | `192.168.64.x/24`, `default via 192.168.64.1` -- correct |
| `wget https://1.1.1.1/` (by IP, no DNS) | fails |
| `nc -z 9.9.9.9 53` | fails |
| UDP to `8.8.8.8:53` | send succeeds, no reply |
| DNS via `--dns 8.8.8.8`, VPN resolver, LAN gateway | all time out |
| Guest -> host TCP (`192.168.64.1:<port>`) | **works** |
| The same external probes from the host | work |

So the break is specifically outbound NAT off the vmnet bridge, not DNS and not
routing.

## The usual cause

Host-level packet filtering. Endpoint-security suites install network/socket
filter system extensions (EDR agents, VPN clients, enterprise firewalls) that
drop traffic from the bridge subnet. Confirming which one requires
`sudo pfctl -s nat` and reading the active extensions. The macOS firewall in
stealth mode also makes ICMP results useless here, so ignore ping.

## A misleading signal

Image **pulls** always work, because the runtime fetches them host-side. A
successful `container run` is not evidence of guest egress. Test from inside:

```bash
cc-doctor          # does exactly this, and records the verdict
```

## The workaround

Guest -> host TCP works, so a proxy on the host bridge address gives guests full
HTTPS:

```bash
brew install tinyproxy
cc-doctor          # records "proxy"; CC_PROXY=auto then uses it
```

`cc-doctor` writes its verdict to `~/.local/state/cc-container/egress`.
`CC_PROXY=auto` (the default) reads that file, so the proxy only starts on
machines that need it, and the launch path stays fast everywhere else. Force it
either way with `CC_PROXY=on` / `CC_PROXY=off`.

## Consequences of running behind the proxy

Anything that hard-codes a direct connection and ignores `http_proxy` /
`https_proxy` will fail. `curl`, `wget`, `git`, `npm`, `yarn`, `pnpm`, `gh` and
Claude Code itself all honour it.

**Node's global `fetch` does not.** A stdio MCP server written against bare
`fetch` will start, list its tools, and then fail every call that touches the
network. Node 22.23+ accepts `NODE_USE_ENV_PROXY=1`; set it per server:

```json
"env": { "NODE_USE_ENV_PROXY": "1" }
```

If your NAT is ever fixed, set `CC_PROXY=off` and nothing else changes; no other
part of the tool depends on the proxy.
