# BIRD Control Socket Wire Protocol — Research Report

*Author: explore sub-agent. Task ID: 2.*
*Date: 2026-08-24*

This document describes the protocol spoken between `birdc` / `birdcl` (the
BIRD command-line clients) and the `bird` daemon over its Unix control
socket. It is based on a close reading of the BIRD source tree at
`/home/z/my-project/birdsrc/bird/` (source version **2.19.0**, see
`VERSION`) plus live confirmation against the installed binary at
`/home/z/my-project/birdbin/usr/sbin/bird` (binary version **3.1.7**, see
`bird --version`). All file:line references are relative to the source
tree root, `/home/z/my-project/birdsrc/bird/`.

> **Version mismatch caveat.** The shipped binary is BIRD 3.1.7 while the
> source tree we have is 2.19.0. The wire protocol is identical between
> these versions in everything we tested (line framing, reply-code prefix
> characters, async `+` prefix, hello banner). However, **some response
> payloads differ** (e.g. 3.1.7 emits `Created:` lines under
> `show protocols all` which 2.19 does not), and a few CLI grammar rules
> appear to differ in 3.x (notably `reload <pattern>` and `echo …` reject
> patterns that are valid in 2.19). Where a claim is purely from the
> source we say "source"; where we confirmed on the wire we say "live".

---

## 1. Control Socket Setup (Server Side)

### 1.1 Where the socket is created

The single primary control socket is created during daemon startup in
`sysdep/unix/main.c`:

| Step | File:line | Notes |
|---|---|---|
| Default config declared | `sysdep/unix/main.c:423–426` | `initial_control_socket_config` with `.name = PATH_CONTROL_SOCKET`, `.mode = 0660` |
| `path_control_socket` macro | `sysdep/unix/main.c:427` | `#define path_control_socket initial_control_socket_config.name` |
| `-s <sock>` option | `sysdep/unix/main.c:969–972` | overrides `path_control_socket` |
| `-l` option (local files) | `sysdep/unix/main.c:985–990` | sets path to basename of default |
| Startup | `sysdep/unix/main.c:1043–1045` | `test_old_bird(path); cli_init_unix(use_uid, use_gid);` |
| `cli_init_unix()` | `sysdep/unix/main.c:620–633` | Sets `use_uid/use_gid`; calls `cli_listen()`; dies if listen fails (`"Won't run without control socket"`) |
| `cli_listen()` | `sysdep/unix/main.c:566–609` | `sk_new`, `s->type = SK_UNIX_PASSIVE`, `rbsize = 1024`, `fast_rx = 1`, `unlink(cf->name)` then `sk_open_unix()`, optional `chown()` to `cf->uid/gid`, `chmod(cf->name, cf->mode)` (default 0660) |
| `sk_open_unix()` | `sysdep/unix/io.c:1745–1775` | `socket(AF_UNIX, SOCK_STREAM, 0)`, `fcntl(O_NONBLOCK)`, `bind()` to `sockaddr_un` with `SUN_LEN(&sa)`, `listen(fd, 8)` (backlog 8) |
| Accept handler | `sysdep/unix/main.c:548–564` `cli_connect()` | installs `cli_rx`/`cli_tx`/`cli_err` hooks, calls `cli_new()` |
| Per-connection `cli` struct | `nest/cli.c:305–327` `cli_new()` | allocates pool, sets `c->cont = cli_hello`, allocates `rx_buf` of `CLI_RX_BUF_SIZE` (4096) bytes |

### 1.2 Default socket path

Hard-coded at build time as `PATH_CONTROL_SOCKET` (a quoted string written
into `obj/sysdep/paths.h` by `Makefile.in:169`):

```make
$(Q)echo >>$@ "#define PATH_CONTROL_SOCKET \"@CONTROL_SOCKET@\""
```

`@CONTROL_SOCKET@` is `$(runstatedir)/bird.ctl` per `configure.ac:118`
(`CONTROL_SOCKET="\$(runstatedir)/bird.ctl"`). For the Debian package
(`distro/pkg/deb/rules:12`) `--runstatedir=/run/bird`, giving the
default `/run/bird/bird.ctl`.

Verified live by inspecting the binary:
`strings /home/z/my-project/birdbin/usr/sbin/bird | grep bird.ctl`
returns `/run/bird/bird.ctl`.

### 1.3 Socket family / type

* `AF_UNIX`, `SOCK_STREAM` (confirmed by source `sysdep/unix/io.c:1753`
  and `sysdep/unix/main.c` for client side `client/client.c:251`).
* Non-blocking I/O (`O_NONBLOCK`) on both ends.
* Listen backlog = 8 (`sysdep/unix/io.c:1769`).
* Per-connection receive buffer `s->rbsize = 1024` bytes
  (`sysdep/unix/main.c:576`).

### 1.4 Access control

There is **no authentication, no TLS, no passwords**. Access control is
purely filesystem-permission based:

* The socket file is `chmod(cf->name, cf->mode)` with default mode
  `0660` (`rw-rw----`) — `sysdep/unix/main.c:425, 595–599`.
* The file is `chown`-ed to `cf->uid` / `cf->gid` (only if non-zero)
  — `sysdep/unix/main.c:588–593`. By default `uid = gid = 0` so no
  `chown` is done; the socket inherits the daemon's owning user/group.
* If BIRD is started with `-u <user>` / `-g <group>`, the chown is
  applied **before** the `drop_uid()` / `drop_gid()` calls
  (`sysdep/unix/main.c:1047–1051`), so the socket is owned by the
  unprivileged user/group that BIRD drops to.

### 1.5 Multiple sockets (control vs. listener)

There is exactly **one** primary listener created at startup
(`main_control_socket` in `sysdep/unix/main.c:440`), but the
configuration file may declare **additional** listener sockets using the
`cli "<path>" [{ restrict; }];` syntax (`sysdep/unix/config.Y:132–153`).
Each declared `cli` block creates a new `struct cli_config` that is
materialized by `cli_commit()` (`sysdep/unix/main.c:648–678`) —
`cli_listen(c)` is called for new ones and `cli_deafen(l)` removes
stale ones after reconfiguration.

The `cli_opts_block` allows the `restrict;` sub-statement
(`sysdep/unix/config.Y:150–153`) which marks every connection on that
socket as restricted (read-only commands). The same flag can also be
enabled per-session by the `restrict` CLI command (`nest/config.Y:971`).

A `cli` struct can also register as a *log hook* via
`cli_set_log_echo()` (`nest/cli.c:340–358`) — the daemon then pushes
log lines into a per-session ring buffer which is sent to the client
as asynchronous `+`-prefixed messages (see §2.4).

---

## 2. Wire Protocol Between `birdc` and `bird`

### 2.1 Framing — line-oriented, NOT length-prefixed

The protocol is **line-oriented**, framed by `\n` (LF, 0x0A).

* Client side: `client/client.c:415–441` `server_send()` — appends `'\n'`
  to the command string and writes raw bytes. No length prefix, no
  escape, no quoting on the wire (the user's quoting in the shell is
  resolved by the lexer on the server).
* Server side: `sysdep/unix/main.c:485–518` `cli_get_command()` — reads
  from `s->rbuf` up to `s->rpos`, **ignores `\r`** (CR, 0x0D) and treats
  `\n` as the line terminator. So commands can be sent with `\n` or
  `\r\n` terminators; a bare `\r` is silently dropped.
* Per-line maximum length: `CLI_RX_BUF_SIZE - 2 = 4094` bytes of
  command text (excluding the terminating `\n`) — see `nest/cli.h:18`
  and `sysdep/unix/main.c:492`. If a single command exceeds this, the
  server returns `9000 Command too long\n` (`nest/cli.c:296–297`,
  `nest/cli.h` code `9000` in `doc/reply_codes`). **Live caveat:** on
  BIRD 3.1.7 we observed that a single 4095-byte send **resets the
  connection** instead of returning `9000`; the documented 2.19
  behaviour may be different or there is a separate socket-buffer issue
  (see §6.2 for details).

The client read buffer is 4096 bytes (`client/client.c:39, 47`). If the
client ever receives a line longer than its buffer, it substitutes the
literal string `?<too-long>` (`client/client.c:341–345`).

### 2.2 Reply line format — 4-digit zero-padded code + separator + text

Every reply line has the form:

```
<NNNN><sep><text>\n
```

where:
* `NNNN` is a 4-digit, **zero-padded** decimal code in `[0000, 9999]`.
* `sep` is a single byte that is one of:
  * **`0x20` SPACE** — this is the **last line** of the response.
  * **`0x2D `-`** — the response **continues** on the next line.
* `text` is arbitrary printable bytes (may be empty).
* The line ends with `\n` (LF, 0x0A).

This format is constructed by `cli_vprintf()` in `nest/cli.c:119–158`:

```c
if (cd < 0) {                          /* continuation */
  cd = -cd;
  if (cd == c->last_reply)
    size = bsprintf(buf, " ");          /* same code → single space prefix */
  else
    size = bsprintf(buf, "%04d-", cd);  /* different code → "NNNN-" prefix */
  errcode = -8000;
}
else if (cd == CLI_ASYNC_CODE) {        /* async, code == 10000 */
  size = 1; buf[0] = '+';               /* single '+' prefix */
  errcode = cd;
}
else {                                  /* final line */
  size = bsprintf(buf, "%04d ", cd);    /* "NNNN " prefix */
  errcode = 8000;
  cd = 0;                               /* last_reply becomes 0 */
}
c->last_reply = cd;
cnt = bvsnprintf(buf+size, sizeof(buf)-size-1, msg, args);  /* append the text */
...
buf[size++] = '\n';                    /* append LF */
```

Key constants:
* `CLI_ASYNC_CODE = 10000` (`nest/cli.h:74`).
* Per-line max length is `CLI_LINE_SIZE = 512` (`nest/cli.h:23`).
  If `bvsnprintf` returns -1 (overflow), the server replaces the
  line with `<NNNN> <line overflow>\n` (`nest/cli.c:150–153`).

### 2.3 Continuation lines

When a multi-line response is being constructed, every line uses a
**continuation** code (a *negative* code passed to `cli_printf`/`cli_msg`).
The prefix logic is:

| Current line code | Previous reply code (`c->last_reply`) | Wire prefix | Example |
|---|---|---|---|
| `-N` (continuation) | `!= N` (any different) | `NNNN-` (5 bytes) | `1007-Table master4:` |
| `-N` (continuation) | `== N` (same as previous) | ` ` (single SPACE, 1 byte) | ` 0.0.0.0/0  unicast …` |
| `+N` (`CLI_ASYNC_CODE`) | (does not update last_reply meaningfully) | `+` (1 byte) | `+Config timeout expired, starting undo` |
| `N` ≥ 0 (final) | resets to 0 | `NNNN ` (5 bytes) | `0000 ` (empty msg) |

So when the server emits many lines with the same code (the common case
for tables, route lists, etc.), only the **first** line carries the
`NNNN-` prefix; all subsequent same-code lines collapse the 5-byte
prefix down to a single SPACE.

### 2.4 End-of-response marker (the most important byte)

**The end-of-response marker is a single SPACE character `0x20` in the
5th byte position of a line** (i.e. the line starts with `NNNN ` — four
digits followed by a space).

Concretely, the canonical "command finished, no error" terminator sent
by the server is:

```
30 30 30 30 20 0a       (ASCII: "0000 \n")
```

— six bytes: ASCII `0`, `0`, `0`, `0`, SPACE, LF. Confirmed live for
every successful command (e.g. `enable direct1`, `show status`, `dump
resources …`, `show protocols`).

When the command finishes with a specific status message, the final
line carries a non-zero code with a SPACE separator, e.g. `0003
Reconfigured\n` (7 bytes? actually `0` `0` `0` `3` ` ` `R` … `\n`) or
`0013 Daemon is up and running\n`. The client treats **any** line of the
form `NNNN ` (code 0–9999, 5th byte = SPACE) as the terminator and
unblocks the prompt — see `client/client.c:280–294`:

```c
else if (strlen(x) > 4 &&
         sscanf(x, "%d", &code) == 1 && code >= 0 && code < 10000 &&
         (x[4] == ' ' || x[4] == '-'))
{
  if (code) PRINTF(len, "%s\n", verbose ? x : x+5);
  last_code = code;
  if (x[4] == ' ') { busy = 0; skip_input = 0; return; }   /* terminator */
}
```

### 2.5 Continuation / separator / table-boundary bytes

There are no other "magic" control characters beyond the three
prefixes above (`+`, ` `, `NNNN-`). Specifically:

* **No 0x1F (Unit Separator), 0x1E (Record Separator), or other ASCII
  control bytes** appear in the wire protocol. Every "control" byte is
  either `+`, ` ` (SPACE, 0x20), `-` (0x2D), or one of the ASCII digits
  0–9. The user's mention of `0x1f` / `0x20` appears to refer to
  SPACE (0x20), which is the end-of-response marker.
* **Section / table separators**: the server uses a 0-byte-text
  continuation line of the form ` \n` (2 bytes: SPACE + LF) or
  `NNNN-\n` (6 bytes) to render blank lines between sections. See
  `cli_separator()` in `nest/cli.h:91–92`:

  ```c
  static inline void cli_separator(cli *c)
  { if (c->last_reply) cli_printf(c, -c->last_reply, ""); };
  ```
  This emits a continuation line with **the same code as the previous
  line** and an **empty message** — which on the wire is ` \n`
  (SPACE + LF) because `cd == c->last_reply` triggers the single-space
  prefix. Used by `proto_iterate_named()` (`nest/proto.c:2442`) and
  several others.
* Direct calls to `cli_msg(-NNNN, "")` with a *different* code from
  the previous line produce `NNNN-\n` (5-byte prefix + LF). Used e.g.
  in `proto_cmd_show()` after each protocol's verbose block
  (`nest/proto.c:2216`).
* Some commands use `\t` (TAB, 0x09) inside the *text* payload — e.g.
  `show interfaces` `cli_msg(-1003, "\t%I/%d …")` in
  `nest/iface.c:845`. This is **not** a protocol separator; it's just
  formatting whitespace inside the message text.

### 2.6 Async / unsolicited messages

Async messages are prefixed with `+` (PLUS, 0x2B) and **do not**
terminate a command response — they arrive on their own at any time
(e.g. when a config-undo timer fires, or as a "Restricting access"
notice, or as live log lines via the `echo` command).

`client/client.c:276–277`:
```c
if (*x == '+')                        /* Async reply */
  PRINTF(len, ">>> %s\n", x+1);
```

Confirmed live: after `configure "/tmp/bird-test.conf" timeout 1\n`
the server first returned the normal command response ending with
`0003 Reconfigured\n`, then one second later sent an unsolicited
`+Config timeout expired, starting undo\n` (35 bytes, prefix `+`,
message text, LF).

### 2.7 Initial hello banner

On every new connection the server's `cli_new()` sets `c->cont =
cli_hello` (`nest/cli.c:317`). On the next event loop tick,
`cli_event()` calls `c->cont(c)` → `cli_hello()` which calls
`cli_printf(c, 1, "BIRD " BIRD_VERSION " ready.")` (`nest/cli.c:198–202`).

On the wire this is a **single final line**:

```
30 30 30 31 20 42 49 52 44 20 33 2e 31 2e 37 20 72 65 61 64 79 2e 0a
"0001 BIRD 3.1.7 ready.\n"
```

(23 bytes for BIRD 3.1.7.) The client (`client/client.c:50–51, 354`)
starts in `init=1, busy=1` and treats this single final line as the
end of the initial "busy" state, then invokes `init_commands()` which
submits either `restrict\n` (if `-r` was passed), the user's initial
command (if any non-option argv), or starts the interactive prompt.

### 2.8 Error vs. normal end

Both error and success are encoded purely in the numeric code:

| Code range | Meaning (`doc/reply_codes`) | Live example |
|---|---|---|
| `0xxx` | Action successfully completed (final) | `0000 \n`, `0003 Reconfigured\n`, `0020 Configuration OK\n` |
| `1xxx` | Table entry (continuation) | `1007-Table master4:`, `1002-device1 …` |
| `2xxx` | Table heading (continuation, table heading) | `2002-Name …`, `2005-Interface …` |
| `8xxx` | Runtime error (final) | `8001 Network not found`, `8002 /file: …`, `8007 Access denied` |
| `9xxx` | Parse-time error (final) | `9001 syntax error, …`, `9001 There is no OSPF protocol running` |
| `10000` | Async message (the `+` prefix, does not terminate a response) | `+Config timeout expired, starting undo` |

So a client distinguishes "error" from "success" purely by whether the
final line's code is in `8xxx`/`9xxx` (error) or `0xxx` (success). The
client stores the final code in `last_code` (`client/client.c:53, 287`)
and exits with status 1 if `last_code >= 8000` for one-shot commands
(`client/client.c:196–201`).

### 2.9 What the client sends — exact bytes

The client's flow for an interactive command is:

1. `input_read()` reads a line from stdin (or readline), strips the
   trailing `\n`.
2. `submit_command()` (`client/client.c:163–175`) calls
   `cmd_expand()` which performs prefix-completion against the static
   command table (built from `conf/commands.h`, generated by
   `conf/gen_commands.m4:10` from the `CF_CLI(...)` macros — keywords
   are transliterated to **lowercase**).
3. `submit_server_command()` (`client/client.c:140–146`) sets
   `busy = 1; num_lines = 2;` and calls `server_send(cmd)`.
4. `server_send()` (`client/client.c:415–441`) computes `l =
   strlen(cmd)`, allocates `l+1` bytes, copies `cmd`, sets `z[l] =
   '\n'`, and writes the `l+1` bytes to the socket with retry on
   `EAGAIN`/`EINTR`.

So the wire bytes for `show status` are exactly:

```
73 68 6f 77 20 73 74 61 74 75 73 0a    ("show status\n", 12 bytes)
```

There is **no quoting, no length prefix, no escaping**. Whatever the
user typed (after expansion) goes to the wire as-is, terminated by a
single LF. For commands that take a string argument, the user must
enclose the string in double quotes — those quotes are sent **on the
wire** and the server-side lexer (`conf/cf-lex.l:346–358`) strips them.
Example: `configure check "/tmp/foo.conf"\n` is sent byte-for-byte
including the `"` characters.

### 2.10 How the client knows when output is complete

The client reads bytes from the socket in 4096-byte chunks
(`client/client.c:39, 314`). The `server_read()` function scans the
buffer for `\n` and calls `server_got_reply()` for each complete line
(`client/client.c:308–346`). `server_got_reply()` (lines 270–305)
implements the dispatcher:

| Wire prefix | Action | Client behaviour |
|---|---|---|
| `+…` | Async | print `>>> …\n`, keep `busy=1` |
| ` …` (leading SPACE) | Continuation line | print `…\n` (or `     …\n` in verbose mode), keep `busy=1` |
| `NNNN-…` (4 digits + `-`) | First line of a multi-line reply with a new code | print `…\n` (or `NNNN-…\n` in verbose mode), keep `busy=1` |
| `NNNN …` (4 digits + SPACE) | Final line | print `…\n` (or `NNNN …\n` in verbose mode), set `busy=0`, return — this unblocks the prompt and any pending input |
| `??? <…>` | Anything else | print `??? <…>\n` (defensive fallback; see `client/client.c:296–297`) |

In verbose mode (`-v` flag) the full prefix is preserved on output; in
non-verbose mode the 5-byte `NNNN-`/`NNNN ` prefix (or 1-byte ` ` for
same-code continuations) is stripped before display.

---

## 3. Commands Supported (and their wire strings)

All commands are declared with the `CF_CLI(...)` macro which is
expanded by `conf/gen_parser.m4:46–49` into a grammar rule of the form
`cli_cmd: cmd_X ; cmd_X: KEYWORD1 KEYWORD2 … ARGS END` where
`KEYWORD1 KEYWORD2` are the uppercase macro tokens. The matching client
command table entry is generated by `conf/gen_commands.m4:10` as the
**lowercase** form `"keyword1 keyword2"` (e.g. `SHOW STATUS` →
`"show status"`).

### 3.1 Top ~20 commands and their wire strings

| # | Wire string (what the client sends, before `\n`) | Source declaration | Handler |
|---|---|---|---|
| 1 | `show status\n` | `nest/config.Y:622` | `cmd_show_status()` — `nest/cmds.c:23–46` |
| 2 | `show memory\n` | `nest/config.Y:625` | `cmd_show_memory()` — `nest/cmds.c:117–144` |
| 3 | `show protocols\n` | `nest/config.Y:628` | `proto_apply_cmd($3, proto_cmd_show, 0, 0)` — `nest/proto.c:2175` |
| 4 | `show protocols all\n` | `nest/config.Y:631` | same with `verbose=1` |
| 5 | `show interfaces\n` | `nest/config.Y:639` | `if_show()` — `nest/iface.c:850` |
| 6 | `show interfaces summary\n` | `nest/config.Y:642` | `if_show_summary()` — `nest/iface.c:894` |
| 7 | `show route\n` | `nest/config.Y:646` | `rt_show($3)` — `nest/rt-show.c:456` |
| 8 | `show route all\n` | `nest/config.Y:654` (option) | `rt_show()` with `verbose=1` |
| 9 | `show route <prefix>\n` | `nest/config.Y:687` | `rt_show()` with `addr` set |
| 10 | `show route for <ip>\n` | `nest/config.Y:693` | `rt_show()` with `addr_mode=RSD_ADDR_FOR` |
| 11 | `show route in <prefix>\n` | `nest/config.Y:699` | `rt_show()` with `addr_mode=RSD_ADDR_IN` |
| 12 | `show route table <t>\n` | `nest/config.Y:706` | `rt_show_add_table()` |
| 13 | `show route export <p>\n` | `nest/config.Y:751` | `rt_show()` with `export_mode=RSEM_EXPORT` |
| 14 | `show route protocol <p>\n` | `nest/config.Y:768` | `rt_show()` with `show_protocol` |
| 15 | `show route stats\n` / `show route count\n` | `nest/config.Y:777/781` | `rt_show()` with `stats=1/2` |
| 16 | `show symbols\n` | `nest/config.Y:870` | `cmd_show_symbols($3)` — `nest/cmds.c:49–70` |
| 17 | `show ospf\n` | `proto/ospf/config.Y:515` | via `proto_get_named()` then OSPF's `show_proto_info` |
| 18 | `show ospf neighbors\n` | `proto/ospf/config.Y:518` | OSPF neighbor dump |
| 19 | `show bfd sessions\n` | `proto/bfd/config.Y:236` | BFD session dump |
| 20 | `show babel neighbors\n` | `proto/babel/config.Y:197` | Babel neighbor dump |
| 21 | `show rip neighbors\n` | `proto/rip/config.Y:202` | RIP neighbor dump |
| 22 | `show static\n` | `proto/static/config.Y:206` | `static_show()` |
| 23 | `show mpls ranges\n` | `nest/mpls.Y:194` | MPLS range dump |
| 24 | `restrict\n` | `nest/config.Y:971` | `this_cli->restricted = 1; cli_msg(16, "Access restricted")` |
| 25 | `configure "<file>"\n` | `sysdep/unix/config.Y:169` | `cmd_reconfig($2, RECONFIG_HARD, $3)` |
| 26 | `configure soft "<file>"\n` | `sysdep/unix/config.Y:172` | `cmd_reconfig($3, RECONFIG_SOFT, $4)` |
| 27 | `configure "<file>" timeout <sec>\n` | `sysdep/unix/config.Y:176` (cmd hint) | `cmd_reconfig` with timeout; also schedules async `+Config timeout expired, starting undo` |
| 28 | `configure confirm\n` | `sysdep/unix/config.Y:179` | `cmd_reconfig_confirm()` |
| 29 | `configure undo\n` | `sysdep/unix/config.Y:182` | `cmd_reconfig_undo()` |
| 30 | `configure status\n` | `sysdep/unix/config.Y:185` | `cmd_reconfig_status()` — `sysdep/unix/main.c:398–416` |
| 31 | `configure check "<file>"\n` | `sysdep/unix/config.Y:188` | `cmd_check_config($3)` — `sysdep/unix/main.c:308–320` |
| 32 | `reload <pattern>\n` | `nest/config.Y:942` (2.19) | `proto_apply_cmd($2, proto_cmd_reload, 1, CMD_RELOAD)` |
| 33 | `reload in <pattern>\n` | `nest/config.Y:944` | `… CMD_RELOAD_IN` |
| 34 | `reload out <pattern>\n` | `nest/config.Y:946` | `… CMD_RELOAD_OUT` |
| 35 | `disable <pattern> [text]\n` | `nest/config.Y:936` | `proto_apply_cmd($2, proto_cmd_disable, 1, …)` |
| 36 | `enable <pattern> [text]\n` | `nest/config.Y:938` | `proto_apply_cmd($2, proto_cmd_enable, 1, …)` |
| 37 | `restart <pattern> [text]\n` | `nest/config.Y:940` | `proto_apply_cmd($2, proto_cmd_restart, 1, …)` |
| 38 | `debug <pattern> <mask>\n` | `nest/config.Y:950` | `proto_apply_cmd($1, proto_cmd_debug, 1, $2)` |
| 39 | `mrtdump <pattern> <mask>\n` | `nest/config.Y:968` | `proto_apply_cmd($2, proto_cmd_mrtdump, 1, $3)` |
| 40 | `dump resources "<file>"\n` | `nest/config.Y:892` | `cmd_dump_file(this_cli, $3, "resources", resource_dump)` — `sysdep/unix/io.c:260` |
| 41 | `dump sockets "<file>"\n` | `nest/config.Y:894` | `… sk_dump_all` |
| 42 | `dump ao keys "<file>"\n` | `nest/config.Y:896` | `… sk_dump_ao_all` |
| 43 | `dump events "<file>"\n` | `nest/config.Y:898` | `… io_log_dump` |
| 44 | `dump interfaces "<file>"\n` | `nest/config.Y:900` | `… if_dump_all` |
| 45 | `dump neighbors "<file>"\n` | `nest/config.Y:902` | `… neigh_dump_all` |
| 46 | `dump attributes "<file>"\n` | `nest/config.Y:904` | `… rta_dump_all` |
| 47 | `dump routes "<file>"\n` | `nest/config.Y:906` | `… rt_dump_all` |
| 48 | `dump protocols "<file>"\n` | `nest/config.Y:908` | `… protos_dump_all` |
| 49 | `dump filter all "<file>"\n` | `nest/config.Y:910` | `… filters_dump_all` |
| 50 | `eval <expr>\n` | `nest/config.Y:913` | `cmd_eval(f_linearize($2, 1))` — `nest/cmds.c:147–159` |
| 51 | `echo (all|off|{…}) [<size>]\n` | `nest/config.Y:917` (2.19) | `cli_set_log_echo(this_cli, $2, $3)` |
| 52 | `timeformat …\n` | `nest/config.Y:975` | `cli_set_timeformat(this_cli, $2)` |
| 53 | `down\n` | `sysdep/unix/config.Y:191` | `cmd_shutdown()` — `sysdep/unix/main.c:737–745` |
| 54 | `graceful restart\n` | `sysdep/unix/config.Y:196` | `cmd_graceful_restart()` — `sysdep/unix/main.c:763–771` |

> **Live caveat (3.1.7 vs 2.19):** On the installed 3.1.7 binary we
> observed that `reload <pattern>` and `echo all|off` are rejected with
> `9001 syntax error, … expecting FILTERS or BGP` and `9001 syntax error,
> unexpected CF_SYM_UNDEFINED` respectively. The 2.19 source grammar
> above clearly accepts them; BIRD 3.x appears to have reorganized
> these rules. The wire framing itself is identical (still
> `NNNN <text>\n`).

### 3.2 Internal (client-only) commands

`client/cmds.Y:9–11` declares three commands handled entirely in the
client without contacting the server:
* `quit` — exits the client (`client/client.c:127–131`)
* `exit` — exits the client (same)
* `help` — prints `Press '?' for context sensitive help.` (line 132–135)

These are intercepted by `handle_internal_command()` in
`client/client.c:124–138` and never sent on the wire.

---

## 4. Response Formats

Below are the byte-level formats of the major commands' responses.
Every example was confirmed against the live 3.1.7 daemon (with the
2.19 source line numbers in parentheses). All examples start with the
initial hello `0001 BIRD <ver> ready.\n` already drained.

### 4.1 `show status` (`nest/cmds.c:23–46`)

Wire response:
```
1000-BIRD <version>\n
1011-Router ID is <router_id>\n
 Hostname is <hostname>\n
 Current server time is <YYYY-MM-DD HH:MM:SS.mmm>\n
 Last reboot on <YYYY-MM-DD HH:MM:SS.mmm>\n
 Last reconfiguration on <YYYY-MM-DD HH:MM:SS.mmm>\n
[<graceful-restart info lines, code -24>]
0013 Daemon is up and running\n        (or 0013 Shutdown in progress / 0013 Reconfiguration in progress)
```

Live example (truncated):
```
1000-BIRD 3.1.7
1011-Router ID is 10.0.0.1
 Hostname is c-6a8c3c11-145d6674-da1004003283
 Current server time is 2026-08-24 13:07:54.017
 Last reboot on 2026-08-24 13:07:46.670
 Last reconfiguration on 2026-08-24 13:07:46.670
0013 Daemon is up and running
```

Notes:
* First line: `1000-` (continuation, code 1000 = "BIRD version").
* Second line: `1011-` (continuation, code 1011 = "Uptime" — repurposed
  for several status fields).
* Following lines collapse to ` ` prefix because they share code 1011.
* Final line: `0013 ` (code 13 = "Status report"). If the daemon is
  shutting down or reconfiguring, the same code 13 with different text
  is used (`Shutdown in progress`, `Reconfiguration in progress`).
* If a configuration undo timer is pending, an extra `0022-Configuration
  unconfirmed, undo in <t> s\n` continuation line is emitted before the
  final `0013 ` line — see `sysdep/unix/main.c:412–413`.

### 4.2 `show protocols` / `show protocols all` (`nest/proto.c:2175–2218`)

`show protocols`:
```
2002-Name       Proto      Table      State  Since         Info\n
1002-<name>     <proto>    <table>    <state> <since>  <info>\n
 …more rows…
0000 \n
```

Live example (3 protocols: device1, kernel1, direct1):
```
2002-Name       Proto      Table      State  Since         Info
1002-device1    Device     ---       up     13:07:46.670
 kernel1    Kernel     master4   up     13:07:46.670
 direct1    Direct     ---       down   13:07:46.670
0000
```
(Final line is the 6 bytes `30 30 30 30 20 0a` = `0000 \n`.)

`show protocols all` adds (for each protocol) a verbose block with code
`1006`:
```
1002-<name> <proto> <table> <state> <since>  <info>\n
1006-  Created:            <since>\n        (3.x; not in 2.19 source)
[1006-  Description:    <desc>\n]   (if p->cf->dsc)
[1006-  Message:        <msg>\n]    (if p->message)
[1006-  Router ID:      <rid>\n]   (if p->cf->router_id)
[1006-  VRF:            <vrf>\n]    (if p->vrf_set)
[<proto-specific show_proto_info output, all code -1006>]
[1006-  Channel <name>\n
 1006-    State:          <state>\n
 1006-    Table:          <table>\n
 1006-    Preference:     <pref>\n
 1006-    Input filter:   <filter>\n
 1006-    Output filter:  <filter>\n
 [<1006-    <Limit>:     <val> [HIT]\n>…]
 [<1006-    Routes:         <imp> imported, [<filt> filtered,] <exp> exported, <pref> preferred\n
  1006-    Route change stats:     received   rejected   filtered    ignored   accepted\n
  1006-      Import updates:     <5 cols>\n
  1006-      Import withdraws:   <cols>\n
  1006-      Export updates:     <cols>\n
  1006-      Export withdraws:   <cols>\n>]
]
1006-\n                       (blank separator line, different code → "NNNN-" prefix + LF)
```

Field widths (`nest/proto.c:2181, 2188`):
* Name, Proto, Table columns: `%-10s ` (10 chars + space)
* State: `%-6s ` (6 chars + space)
* Since: `%-12s  ` (12 chars + 2 spaces)
* Info: `%s`

### 4.3 `show memory` (`nest/cmds.c:117–144`)

Wire response:
```
1018-BIRD memory usage\n
                 Effective    Overhead\n           (header line, code -1018, empty-name col)
 Routing tables:    <eff>     <ovh>\n          (each line is code -1018)
 Route attributes:   <eff>     <ovh>\n
 Protocols:         <eff>     <ovh>\n
 Current config:    <eff>     <ovh>\n
 Standby memory:    <eff>     <ovh>\n
 Total:             <eff>     <ovh>\n
1018-\n                                   (blank separator, same code as last → " \n" on wire)
 Active pages:      <val>\n
 Kept free pages:   <val>\n
 Cold free pages:   <val>\n
0000 \n
```

Field layout (`nest/cmds.c:108`): `%-17s " SIZE_FORMAT "   " SIZE_FORMAT` where
`SIZE_FORMAT = "% 4u.%1u % 1cB"` (4-digit integer, 1 decimal, 1 unit
suffix from `" kMGT"`). Example wire line: ` Routing tables:    73.7 kB    9.9 kB\n`.

### 4.4 `show interfaces` / `show interfaces summary` (`nest/iface.c:850–921`)

`show interfaces` per-interface:
```
1001-<ifname> <up|down> (index=<n>[ master=<master>])\n
1004-\t<Type> [Broadcast] [Multicast] Admin<Up|Down> Link<Up|Down> [Loopback] [Ignored] MTU=<mtu>\n
[1003-\t<ip>/<pxlen> (<flags>, scope <scope>)\n]…
0000 \n
```

`show interfaces summary`:
```
2005-Interface   State  IPv4 address       IPv6 address\n
1005-<name>     <up|dn> <v4 or blank>     <v6 or blank>\n
 …
0000 \n
```

### 4.5 `show route` family (`nest/rt-show.c`)

`show route` (default master4 table):
```
1007-Table <tablename>:\n
 <prefix>          <dest-name> [<proto> <time>] [*|!] (<pref>)\n    (primary route marker: * = primary, ! = sync error)
 \tvia <gw> on <iface>[ mpls <labels>][ onlink][ weight N]\n
 [\tdev <iface>[ mpls …][ onlink][ weight N]\n]
 [<extra routes for same prefix>]
[1007-\n]                       (blank separator before next table)
0000 \n                          (or 0014 Total: … for multi-table stats)
```

If no prefix is found: `8001 Network not found\n` (single final line,
code 8001).

`show route all` adds an attribute dump section using code `1012`:
```
…<route line as above>…
1012-\tpreference: <n>\n
 \tsource: <source>\n
 \t<attr_name>: <value>\n
 …
1008-\tInternal route handling values: <n>L <n>G <n>S id <id>\n
0000 \n
```

### 4.6 `show symbols` (`nest/cmds.c:49–70`)

```
1010-<name>     \t<class>\n
 <name>     \t<class>\n
 …
0000 \n
```

Field width: `%-8s\t%s` (`nest/cmds.c:64`). Symbol classes include
`protocol`, `routing table`, `filter`, `function`, `custom attribute`,
`undefined`, `unknown type`.

### 4.7 `restrict`

Single-line final response: `0016 Access restricted\n` (23 bytes).
This sets `this_cli->restricted = 1` (`nest/config.Y:972`) which causes
subsequent privileged commands to fail with `8007 Access denied`
(checked by `cli_access_restricted()` in `nest/cli.h:103–109`).

### 4.8 `eval <expr>` (`nest/cmds.c:147–159`)

* Success: `0023 <value>\n` (single final line, code 23).
* Runtime error: `8008 runtime error\n` (single final line, code 8008).

### 4.9 `configure check "<file>"` (`sysdep/unix/main.c:308–320`)

Success:
```
0002-Reading configuration from <file>\n
0020 Configuration OK\n
```

Failure (file missing, parse error, etc.):
```
0002-Reading configuration from <file>\n
8002 <file>:<lino>:<chno> <error>\n         (or 8002 <file>: <strerror>)
```

### 4.10 `configure "<file>" [timeout <sec>]` (`sysdep/unix/main.c:353–373`)

Success without timeout:
```
0002-Reading configuration from <file>\n
0003 Reconfigured\n
```

Success with `timeout <sec>`:
```
0002-Reading configuration from <file>\n
0022-Undo scheduled in <sec> s\n
0003 Reconfigured\n
…wait <sec>…
+Config timeout expired, starting undo\n     (async, after delay)
```

Other final codes from `cmd_reconfig_msg()` (`sysdep/unix/main.c:323–336`):
`0004 Reconfiguration in progress`, `0005 Reconfiguration already in progress, queueing new config`,
`0017 Reconfiguration already in progress, removing queued config`, `0018 Reconfiguration confirmed`,
`0006 Reconfiguration ignored, shutting down`, `0019 Nothing to do`.

### 4.11 `enable|disable|restart|reload <pattern>` (per-protocol actions)

Each matched protocol emits a continuation line with the appropriate
code, then a final `0000 \n`:

```
[0010-<name>: already disabled\n]    (if applicable, code 0010)
[0009-<name>: disabled\n]              (code 0009)
[0010-<name>: already enabled\n]      (code 0010)
[0011-<name>: enabled\n]              (code 0011)
[0012-<name>: restarted\n]            (code 0012)
[0015-<name>: reloading\n]            (code 0015)
[0008-<name>: already disabled\n]     (code 0008, used in disable/restart/reload when already down)
[8006-<name>: reload failed\n]         (code 8006)
0000 \n
```

If no protocols match: `8003 No protocols match\n` (single final line).

### 4.12 `dump <kind> "<file>"` (`sysdep/unix/io.c:260–272`)

Triggers a background dump to the file. The wire response is a series
of continuation lines reporting per-resource progress, then a final
status line:
```
0013-Dumping <kind> to <file>\n
 ... dumped <N> bytes in <T> s\n
 ... dumped <N> bytes in <T> s\n
 …
0013 Dumped <total> bytes in <T> s\n
```
(Each `...` is part of the message text, not a separate prefix.)

### 4.13 `down` / `graceful restart`

* `down` → `0007 Shutdown requested\n` then the daemon exits and the
  connection is closed (we did not test live to avoid killing the
  daemon).
* `graceful restart` → `0025 Graceful restart requested\n` then the
  daemon performs a graceful-restart shutdown.

### 4.14 `timeformat …` (`nest/cli.c:407–434`)

Always replies `0000 \n` (single final line, code 0). On error:
`9003 Format string too long\n`.

---

## 5. End-of-Response Markers (Summary Table)

| Marker (wire bytes) | Meaning |
|---|---|
| `NNNN \n` (4 digits, SPACE, optional text, LF) | **Final line of a command response.** Code 0–9999. Unblocks the client. |
| `NNNN-\n` or `NNNN-<text>\n` (4 digits, DASH, optional text, LF) | Continuation line, **first** line of a new sub-section that uses a different code than the previous line. |
| ` <text>\n` or ` \n` (single SPACE, optional text, LF) | Continuation line, **same code** as the previous line. Also the form produced by `cli_separator()`. |
| `+<text>\n` (PLUS, text, LF) | Async / unsolicited message. Does **not** terminate a command response; arrives at any time. |

The "canonical end-of-success" byte sequence is `30 30 30 30 20 0a`
(`0000 \n`, 6 bytes). The "canonical end-of-error" sequence has the
form `38 30 0… 0 20 … 0a` or `39 30 0… 0 20 … 0a` (i.e. `8xxx ` or
`9xxx `).

---

## 6. Server Busy / Error States

### 6.1 Unknown command (parse error)

Wire: `9001 <error message>\n` — single final line. Example live
responses:

* `garbage command here` →
  `9001 syntax error, unexpected GARBAGE\n`
* `show status extra args` →
  `9001 syntax error, unexpected CF_SYM_UNDEFINED, expecting END\n`
* `dump resources /tmp/dump.txt` (bare path, must be quoted) →
  `9001 syntax error, unexpected '/', expecting CF_SYM_KNOWN or TEXT\n`
* `show ospf` (no OSPF protocol running) →
  `9001 There is no OSPF protocol running\n`
  (this is from `cf_error()` in `proto_get_named()` at
  `nest/proto.c:2407`; `cli_command()` then formats it as a 9001
  reply — `nest/cli.c:272`)

### 6.2 Command too long

Source: `nest/cli.c:296–297` returns `9000 Command too long\n` when
`cli_get_command()` returns -1 because the line exceeded `CLI_RX_BUF_SIZE
- 2 = 4094` bytes.

**Live behaviour (BIRD 3.1.7):** Sending a 4095-byte or longer line
(single send or chunked) caused the daemon to **reset the connection**
(`ConnectionResetError: [Errno 104] Connection reset by peer`) instead
of returning `9000 Command too long`. We could not reproduce the
`9000` reply on 3.1.7. The 2.19 source path may have a separate
behaviour, or 3.x may have changed the buffer-overflow handling. Either
way, a robust client must handle both: (a) a `9000 Command too long\n`
final line, or (b) a sudden connection reset.

### 6.3 Runtime errors

Runtime errors are final lines with code `8xxx`:

| Code | Message | Source |
|---|---|---|
| 8000 | (catch-all for `<line overflow>` during formatting) | `nest/cli.c:152, 134, 144` |
| 8001 | `Network not found` | `nest/rt-show.c:497` |
| 8002 | `<file>:<lino>:<chno> <err>` or `<file>: <strerror>` | `sysdep/unix/main.c:298–300` |
| 8003 | `No protocols match` | `nest/proto.c:2364` |
| 8004 | `Stopped due to reconfiguration` | `nest/rt-show.c:256` |
| 8005 | `Protocol is down => cannot dump` | `doc/reply_codes` |
| 8006 | `<name>: reload failed` | `nest/proto.c:2295` |
| 8007 | `Access denied` | `nest/cli.h:106` |
| 8008 | `runtime error` | `nest/cmds.c:154` |
| 8009 | `Failed to open file` | `doc/reply_codes` |

### 6.4 Daemon not running

If no BIRD is listening on the socket path, the client's `connect()`
fails immediately with `ECONNREFUSED` and the client exits with `DIE
("Unable to connect to server control socket (%s)", server_path)` —
`client/client.c:261–262`.

If a *stale* socket file exists (e.g. bird was killed without unlinking
the socket), `connect()` also fails with `ECONNREFUSED` because there is
no listener bound to the path (Unix socket semantics: a file with no
listener returns `ECONNREFUSED` on `connect()`).

Before bind, BIRD calls `unlink(cf->name)` to remove any stale socket
file (`sysdep/unix/main.c:580`), and `test_old_bird()` refuses to start
if `connect()` to the path succeeds — i.e. another BIRD is already
running (`sysdep/unix/io.c:2664–2681`).

### 6.5 Partial output / paged output

The server-side route walker yields after every 64 routes
(`nest/rt-show.c:249`, `max = 64` — 4 in DEBUGGING builds). It uses the
`c->cont` continuation hook (`nest/cli.h:38`) — `rt_show_cont()` in
`nest/rt-show.c:242`. Between chunks, the response is **not** terminated;
the `cont` mechanism simply re-enters the event loop and produces more
continuation lines with the same `1007` code, until either the walk
completes (final `0000 \n`) or a runtime error occurs (e.g.
`8004 Stopped due to reconfiguration`).

There is no explicit "more" marker between chunks; the client can't
tell chunk boundaries from the wire, only the final `0000 ` line.

### 6.6 Paged terminal output (client-side only)

The interactive client (`birdc`) implements its own pager
(`client/client.c:214–241` `more()`), which prints `--More--` and waits
for the user to press SPACE, ENTER, or `q` to continue. This is purely
client-side display behaviour and is **not** part of the wire protocol.

---

## 7. Live Behaviour — Confirmed Wire Bytes

All of the following were captured with a small Python Unix-socket
sniffer (`/home/z/my-project/scripts/sniff_bird.py`) against a BIRD 3.1.7
daemon at `/tmp/bird-test.ctl` with a minimal config (`/tmp/bird-test.conf`,
device + kernel + direct protocols). The daemon was started with
`setsid /home/z/my-project/birdbin/usr/sbin/bird -c /tmp/bird-test.conf
-s /tmp/bird-test.ctl -P /tmp/bird-test.pid`.

| Command sent | Wire bytes sent | First reply bytes (hex) |
|---|---|---|
| (initial connect) | — | `30 30 30 31 20 42 49 52 44 20 33 2e 31 2e 37 20 72 65 61 64 79 2e 0a` = `0001 BIRD 3.1.7 ready.\n` (23 bytes) |
| `show status` | `73 68 6f 77 20 73 74 61 74 75 73 0a` (12 B) | `31 30 30 30 2d 42 49 52 44 20 33 2e 31 2e 37 0a` = `1000-BIRD 3.1.7\n` … `30 30 31 33 20 44 61 65 6d 6f 6e 20 69 73 20 75 70 20 61 6e 64 20 72 75 6e 6e 69 6e 67 0a` = `0013 Daemon is up and running\n` |
| `restrict` | `72 65 73 74 72 69 63 74 0a` (9 B) | `30 30 31 36 20 41 63 63 65 73 73 20 72 65 73 74 72 69 63 74 65 64 0a` = `0016 Access restricted\n` (23 B) |
| `show protocols` | `73 68 6f 77 20 70 72 6f 74 6f 63 6f 6c 73 0a` (15 B) | `32 30 30 32 2d 4e 61 6d 65 …` = `2002-Name …` … `30 30 30 30 20 0a` = `0000 \n` (6 B, terminator) |
| `show protocols all` | `73 68 6f 77 20 70 72 6f 74 6f 63 6f 6c 73 20 61 6c 6c 0a` (19 B) | begins `2002-Name …` then `1002-device1 …` then `1006-  Created: …` (3.x only) … ends `0000 \n` |
| `show memory` | `73 68 6f 77 20 6d 65 6d 6f 72 79 0a` (12 B) | `31 30 31 38 2d 42 49 52 44 20 6d 65 6d 6f 72 79 20 75 73 61 67 65 0a` = `1018-BIRD memory usage\n` … `30 30 30 30 20 0a` = `0000 \n` |
| `show route` | `73 68 6f 77 20 72 6f 75 74 65 0a` (11 B) | `31 30 30 37 2d 54 61 62 6c 65 20 6d 61 73 74 65 72 34 3a 0a` = `1007-Table master4:\n` … `30 30 30 30 20 0a` = `0000 \n` |
| `show route 192.0.2.0/24` | `73 68 6f 77 20 72 6f 75 74 65 20 31 39 32 2e 30 2e 32 2e 30 2f 32 34 0a` (24 B) | `38 30 30 31 20 4e 65 74 77 6f 72 6b 20 6e 6f 74 20 66 6f 75 6e 64 0a` = `8001 Network not found\n` |
| `garbage command here` | `67 61 72 62 61 67 65 20 63 6f 6d 6d 61 6e 64 20 68 65 72 65 0a` (21 B) | `39 30 30 31 20 73 79 6e 74 61 78 20 65 72 72 6f 72 2c 20 75 6e 65 78 70 65 63 74 65 64 20 47 41 52 42 41 47 45 0a` = `9001 syntax error, unexpected GARBAGE\n` |
| `configure check "/nonexistent.conf"` | 38 B | `30 30 30 32 2d 52 65 61 64 69 6e 67 20 63 6f 6e 66 69 67 75 72 61 74 69 6f 6e … 0a 38 30 30 32 20 2f 6e 6f 6e 65 78 69 73 74 65 6e 74 2e 63 6f 6e 66 3a 20 4e 6f 20 73 75 63 68 … 0a` = `0002-Reading configuration from /nonexistent.conf\n8002 /nonexistent.conf: No such file or directory\n` |
| `enable all` | `65 6e 61 62 6c 65 20 61 6c 6c 0a` (11 B) | `30 30 31 30 2d 64 65 76 69 63 65 31 3a 20 61 6c 72 65 61 64 79 20 65 6e 61 62 6c 65 64 0a` = `0010-device1: already enabled\n` … `30 30 30 30 20 0a` = `0000 \n` |
| `disable direct1` | `64 69 73 61 62 6c 65 20 64 69 72 65 63 74 31 0a` (16 B) | `30 30 30 39 2d 64 69 72 65 63 74 31 3a 20 64 69 73 61 62 6c 65 64 0a` = `0009-direct1: disabled\n` … `30 30 30 30 20 0a` = `0000 \n` |
| `eval (1+2)` | `65 76 61 6c 20 28 31 2b 32 29 0a` (11 B) | `30 30 32 33 20 33 0a` = `0023 3\n` (7 B) |
| `dump resources "/tmp/dump.txt"` | 31 B (with quotes) | `30 30 31 33 2d 44 75 6d 70 69 6e 67 20 72 65 73 6f 75 72 63 65 73 20 74 6f 20 2f 74 6d 70 2f 64 75 6d 70 2e 74 78 74 0a` = `0013-Dumping resources to /tmp/dump.txt\n` … `30 30 31 33 20 44 75 6d 70 65 64 20 34 36 39 39 37 20 62 79 74 65 73 20 69 6e 20 30 2e 30 30 30 20 73 0a` = `0013 Dumped 46997 bytes in 0.000 s\n` |
| `configure "/tmp/bird-test.conf" timeout 1` | 42 B (with quotes) | `30 30 30 32 2d 52 65 61 64 69 6e 67 … 0a 30 30 32 32 2d 55 6e 64 6f 20 73 63 68 65 64 75 6c 65 64 20 69 6e 20 31 20 73 0a 30 30 30 33 20 52 65 63 6f 6e 66 69 67 75 72 65 64 0a` = `0002-Reading …\n0022-Undo scheduled in 1 s\n0003 Reconfigured\n` then ~1 sec later: `2b 43 6f 6e 66 69 67 20 74 69 6d 65 6f 75 74 20 65 78 70 69 72 65 64 2c 20 73 74 61 72 74 69 6e 67 20 75 6e 64 6f 0a` = `+Config timeout expired, starting undo\n` (async) |

### 7.1 Surprises discovered during live testing

1. **The end-of-response marker is a SPACE, not a `+`/`-` flag.** The
   5th byte of every reply line is either a SPACE (final) or a DASH
   (continuation). There is no separate "end" byte.
2. **The "async" prefix is a literal `+` (0x2B)** at byte 0 of the line.
   It does not interact with `last_reply` and does not terminate any
   response.
3. **Same-code continuation lines collapse to a single SPACE prefix.**
   This means the wire form of a 50-row `show protocols` response has
   only one `1002-` line; the remaining 49 rows each start with a
   single SPACE.
4. **Empty-message continuation lines exist.** `cli_separator()` and
   `cli_msg(-N, "")` produce either ` \n` (2 bytes, when same code as
   previous) or `NNNN-\n` (6 bytes, when different code). The blank
   line between protocols in `show protocols all` is `1006-\n`
   (different code from the preceding `1002` row).
5. **There are no `0x1F` / `0x1E` / other ASCII control bytes** used as
   protocol separators. The only "special" bytes are `+`, ` ` (SPACE),
   `-`, and the digits 0–9. `\t` (TAB) appears in some message texts
   but is not a protocol separator.
6. **BIRD 3.1.7's `proto_cmd_show` adds a `1006-  Created: <time>\n`
   line** under `show protocols all` that is **not present** in the
   2.19 source tree (we grepped the entire source for `Created:` and
   found nothing; `strings bird | grep Created` shows the format
   string exists in the binary). This is a 3.x addition.
7. **BIRD 3.1.7 rejects `reload <pattern>` and `echo …`** with
   `9001 syntax error, … expecting FILTERS or BGP` — the 2.19 grammar
   clearly accepts these. The grammar has been reorganized in 3.x.
8. **Sending a 4095-byte+ command causes the daemon to RESET the
   connection** on 3.1.7, instead of returning `9000 Command too long`
   as the 2.19 source would suggest. A robust client must handle both
   behaviours.
9. **The server's listen backlog is only 8** (`sysdep/unix/io.c:1769`)
   and the per-connection receive buffer is only 1024 bytes
   (`sysdep/unix/main.c:576`). Large commands must be sent in chunks
   small enough to fit the kernel socket buffer; the server's event
   loop drains them in 1024-byte reads.
10. **The hello line uses code `0001`** (positive, final form) — so on
    connect the client immediately receives a single line ending with
    SPACE+LF that **unblocks the initial `busy=1` state**. The client
    then submits either `restrict\n`, the user's init command, or
    starts the interactive prompt.
11. **No authentication at all.** Anyone who can `connect()` to the
    socket file (i.e. anyone with read/write permission on the socket
    inode — typically `rw-rw----` 0660, owned by `bird:bird` or
    `root:bird`) has full control of the daemon. Use the `restrict;`
    option on a `cli "<path>" { restrict; };` config block to expose
    a read-only socket.
12. **The `dump` command requires the file argument to be a quoted
    string** (`"…"`) on the wire — a bare path like
    `dump resources /tmp/dump.txt` is a syntax error
    (`9001 syntax error, unexpected '/', expecting CF_SYM_KNOWN or
    TEXT`).

---

## 8. Quick Reference — Implementing a Client

To implement a minimal BIRD control-socket client:

1. `socket(AF_UNIX, SOCK_STREAM, 0)`, `connect()` to the socket path,
   set `O_NONBLOCK`.
2. Read until you see a line ending in `\n` whose first 4 bytes are
   digits and whose 5th byte is SPACE — that is the hello banner
   (`0001 BIRD <ver> ready.\n`). Discard it (or verify the version).
3. Send `<command>\n` (raw bytes, no length prefix, no escape; the
   command itself is whatever the user typed in lowercase keywords
   with arguments quoted as needed).
4. Read response lines one at a time (split on `\n`):
   * If a line starts with `+`, it is async — print/handle it but do
     **not** unblock.
   * If a line starts with a SPACE, it is a continuation of the previous
     section (strip the leading space, append to current section).
   * If a line starts with 4 digits + DASH (`NNNN-`), it is the first
     line of a new section. Record the code.
   * If a line starts with 4 digits + SPACE (`NNNN `), it is the
     **final line** of the response. Record `last_code = NNNN`; if
     `NNNN >= 8000`, the command failed.
5. After the final line, the connection is idle; send the next
   command. The server may still send async `+` lines at any time
   (including before the next command's first reply).
6. To exit cleanly, just `close()` the socket. There is no goodbye
   message.
7. Max command length: 4094 bytes (excluding `\n`). If you exceed it,
   expect either `9000 Command too long\n` (2.19) or a connection
   reset (3.1.7).
8. Max reply line length: 511 bytes of text + 5 bytes of prefix + 1
   byte LF = 517 bytes (from `CLI_LINE_SIZE = 512` at `nest/cli.h:23`).
   If a single reply would exceed this, the server replaces it with
   `<NNNN> <line overflow>\n`.

---

## 9. Key Source File References (Cheat Sheet)

| Topic | File:line | Function / Symbol |
|---|---|---|
| Client: `server_connect()` | `client/client.c:246–265` | `socket(AF_UNIX, SOCK_STREAM, 0)` |
| Client: `server_send()` | `client/client.c:415–441` | Appends `'\n'`, writes raw bytes |
| Client: `server_got_reply()` | `client/client.c:270–305` | Dispatcher: `+` / ` ` / `NNNN-` / `NNNN ` |
| Client: `server_read()` | `client/client.c:307–346` | Splits on `\n` |
| Client: `init_commands()` | `client/client.c:177–209` | Submits `restrict` or init_cmd |
| Client: prompt / paging | `client/client.c:214–241` | `more()` pager (display only) |
| Server: `cli_new()` | `nest/cli.c:305–327` | Allocates `cli`, sets `c->cont = cli_hello` |
| Server: `cli_hello()` | `nest/cli.c:197–202` | `cli_printf(c, 1, "BIRD " BIRD_VERSION " ready.")` |
| Server: `cli_vprintf()` | `nest/cli.c:119–158` | Constructs `NNNN `, `NNNN-`, ` `, or `+` prefix |
| Server: `cli_event()` | `nest/cli.c:277–303` | Main loop: drains async ring, runs `cont`, reads next cmd |
| Server: `cli_command()` | `nest/cli.c:252–275` | Parses command line via the Yacc grammar |
| Server: `cli_get_command()` | `sysdep/unix/main.c:485–518` | Reads from socket buffer, splits on `\n` |
| Server: `cli_listen()` | `sysdep/unix/main.c:566–609` | Creates listener, `chmod 0660`, optional `chown` |
| Server: `sk_open_unix()` | `sysdep/unix/io.c:1745–1775` | `socket/bind/listen(fd, 8)` |
| Server: `test_old_bird()` | `sysdep/unix/io.c:2664–2681` | Refuses to start if socket already in use |
| Server: `cli_echo()` | `nest/cli.c:361–404` | Pushes async log lines into per-session ring buffer |
| Server: `cli_set_log_echo()` | `nest/cli.c:340–358` | Enables/disables async log echoing |
| Server: `cli_separator()` | `nest/cli.h:91–92` | Blank separator line (` \n` or `NNNN-\n`) |
| Server: `cli_access_restricted()` | `nest/cli.h:103–109` | Returns `8007 Access denied` for restricted sessions |
| Constants | `nest/cli.h:18–23, 74` | `CLI_RX_BUF_SIZE=4096`, `CLI_TX_BUF_SIZE=4096`, `CLI_MAX_ASYNC_QUEUE=4096`, `CLI_MSG_SIZE=500`, `CLI_LINE_SIZE=512`, `CLI_ASYNC_CODE=10000` |
| Reply code list | `doc/reply_codes` | Authoritative table of 0xxx/1xxx/2xxx/8xxx/9xxx codes |
| CLI grammar | `nest/config.Y:620–982`, `sysdep/unix/config.Y:165–197` | All `CF_CLI(...)` declarations |
| CLI grammar expansion | `conf/gen_parser.m4:46–49` | `cli_cmd: cmd_X ; cmd_X: KEYWORDS ARGS END` |
| Client command table | `conf/gen_commands.m4:10` | Lowercase form `"keyword keyword …"` |
| Lexer CLI mode | `conf/cf-lex.l:114, 333–336` | `<CLI>(.|\n) → return CLI_MARKER` |
| Parser CLI mode | `conf/confbase.Y:136, 205–207` | `config: … \| CLI_MARKER cli_cmd` |
| CLI read hook (fake `!` token) | `nest/cli.c:235–250` | `cli_cmd_read_hook()` prepends `!` |
| Protocol show | `nest/proto.c:2175–2218` | `proto_cmd_show()` |
| Route show | `nest/rt-show.c:41–100, 242–358, 456–499` | `rt_show_rte()`, `rt_show_cont()`, `rt_show()` |
| Interface show | `nest/iface.c:833–921` | `if_show()`, `if_show_summary()` |
| Status / memory / symbols | `nest/cmds.c:23–144` | `cmd_show_status()`, `cmd_show_memory()`, `cmd_show_symbols()` |
| Config / shutdown | `sysdep/unix/main.c:308–416, 737–771` | `cmd_check_config()`, `cmd_reconfig*()`, `cmd_shutdown()`, `cmd_graceful_restart()` |
| Dump command | `sysdep/unix/io.c:255–272` | `cmd_dump_file()` |

---

## 10. Open Questions / Limitations

* **BIRD 2.19 vs 3.1.7 differences in command grammar** (notably
  `reload` and `echo`) could not be fully resolved from the 2.19
  source alone; the wire framing is identical, only the set of
  accepted commands differs. The user should consult the 3.x source
  (not available in this sandbox) for the exact 3.1.7 grammar.
* **The 4095-byte connection-reset behaviour** on 3.1.7 may be a 3.x
  regression or a different buffer-size constant; we could not
  confirm the 2.19 source's `9000 Command too long` reply on the live
  binary.
* **The `Created:` line in `show protocols all`** on 3.1.7 has no
  corresponding source line in 2.19; it is a 3.x addition.
* The `echo …` and `mrtdump …` commands register a per-session log
  ring buffer (`nest/cli.c:340–358`); we did not exhaustively test
  the resulting async-message stream, but the format is the same `+`
  prefix documented in §2.6.
* Some protocol-specific show commands (`show ospf`, `show bfd
  sessions`, `show babel neighbors`, `show rip neighbors`, `show
  static`) all returned `9001 There is no <X> protocol running\n`
  on 3.1.7 because no such protocol was configured in our minimal
  test config. The wire framing for the *populated* response would
  be the same (continuation lines + `0000 \n` terminator); we did
  not stand up a full BGP/OSPF/Babel testbed to capture those.

---

*End of report.*

## 7. Security Posture: Line-Framing and Command Injection

### 7.1 The threat

The control protocol executes **every `\n`-terminated line as an independent
command** (§2.1, §2.9). There is no quoting or parameterisation at the protocol
level, so any client that interpolates user-controlled strings into commands is
injectable. Verified live against BIRD 3.1.7 — a single payload of:

```
eval 1+2\nshow status\n
```

produces **two** independent replies (`0023 3` and the `show status` output).
The classic worst case is `show route <prefix>\ndown`, which would shut the
daemon down.

### 7.2 The defence (bird_api.validation, added in v0.2.0)

| Layer | Rule | Defends against |
|---|---|---|
| Line safety | No C0 control characters or DEL in *any* value | newline/command injection (framing attacks) |
| Lexical safety | Identifiers `[A-Za-z_][A-Za-z0-9_]*` ≤ 64 chars; prefixes canonicalised via `ipaddress` with host bits rejected (mirrors the daemon's own `Invalid IPv4 prefix …` error); patterns are printable ASCII without `"`; enums fixed | argument smuggling within one line |
| Structural safety | mutual-exclusion checks; 4094-byte command cap | malformed commands, confusing errors |

Free-form surfaces (`raw()`, `eval()`, `where` expressions) accept the full
filter language but only as a single printable-ASCII line — framing cannot be
broken, so the worst case is a daemon-side syntax error (9001), never a second
executed command.

### 7.3 Read-only mode

`BirdClient(read_only=True)` (and `BirdConnection(read_only=True)`) enforce a
client-side allowlist — `show …`, `configure status`, `restrict` — before any
byte is written. Unlike the server-side `restrict` command (§4.7), the
client-side check survives transparent reconnects. `eval` is deliberately
excluded from the allowlist (it can burn daemon CPU on pathological
expressions and is unnecessary for monitoring).

### 7.4 Grammar facts verified for the query builders (v0.2.0)

* Filter boolean operators are C-style: `&&`, `||`, `!` — the word forms
  `and` / `or` / `not` are **syntax errors** on BIRD 3.1.7.
* `proto = "name"` string comparison works in `where` clauses.
* Protocol patterns must be double-quoted TEXT to use wildcards:
  `show protocols "static*"` (unquoted `static*` is a syntax error);
  `patmatch()` understands `*` and `?`.
* `show route <prefix>` with host bits set is rejected by the daemon with
  a helpful message; the library rejects the same inputs client-side.
* Route lines may carry an optional marker (`*` best, `!` sync error, or
  **absent** for non-primary routes) and BGP appends an info tail such as
  `(100) [i]` / `(100/50) [AS65000e]` (proto/bgp/attrs.c
  `bgp_get_route_info`); `from <ip>` appears inside the source brackets of
  BGP-learned routes.
* Two BIRD instances can peer over loopback without root using
  `local <ip> port 1179 …; strict bind;` on distinct 127/8 addresses
  (used by the e2e BGP tests).
