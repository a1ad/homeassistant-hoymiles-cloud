# Station power limit: observed API behaviour

Captured on 2026-11-10 against a single-phase Belgian station with one
microinverter (HMS-1600-4WB), no battery and no meter. The account exposes
`installer_v3` as well as `web_v3`; all calls below were made with the ordinary
`web_v3` token the integration already obtains.

Everything here is observed client behaviour, not a published API contract.
The source is the manufacturer's public web client,
`https://static.hoymiles.com/cdncache/website/js/` (bundles
`index-COaHho1l.js` and `index-CTUY73vV.js`, both fetched on 2026-11-10), plus
live requests against the account. Treat the numbers as firmware-specific until
they are confirmed on a second installation.

## The value

`POST /pvm/api/0/station/find` returns the station, including:

```json
"config": {
  "power_limit": "50",
  "power_limit_pf": "",
  "power_limit_re": ""
}
```

`power_limit` is the percentage of AC output the cloud allows. For a 1600 W
device, `"50"` is 800 W. `power_limit_pf` and `power_limit_re` are the power
factor and reactive power variants of the same setting (`""` when unused).

This field is the only readback. The write command has no read counterpart, and
the web client refreshes the station detail after closing its drawer instead.

## Write: device command action 8

```http
POST /pvm-ctl/api/0/dev/command/put
{
  "action": 8,
  "data": {"sid": 14091820, "power_limit": 75, "enable": 1}
}
```

The response carries a job id as a bare string:

```json
{"status": "0", "message": "success", "data": "369673500"}
```

Poll the job at `POST /pvm-ctl/api/0/dev/command/put_status` with `{"id": "<job id>"}`:

```json
{
  "status": "0",
  "message": "success",
  "data": {
    "back": 1,
    "code": 0,
    "rate": 100,
    "cfg_data": {"dtu_sn": "4162...", "list": [], "tag": 8710, "type": 1},
    "speed": [
      {"err_code": "", "module": "003000", "rate": 100, "sid": 14091820,
       "sn": "1620...", "station_name": "Marie", "type": 3}
    ]
  }
}
```

`data.code` is the job state:

| code | meaning |
|---|---|
| 0 | success |
| 1 | failed |
| 2 | still running |
| 3 | timeout |

On the observed installation the job runs for about seven seconds: `code: 2`
with `rate: 1` and `speed[].rate: 0`, then `code: 0`, `rate: 100`,
`speed[].rate: 100`. The station detail shows the new percentage immediately
afterwards.

The manufacturer UI offers 2-100 % for active power. Sending the command without
`power_limit` fails fast and is a useful negative control: `code: 1`,
`module: "003000"`, `speed[].rate: -2`.

## Paths that do not apply to this hardware

Documented here so nobody re-derives them.

**Configuration type 515 (configured output upper limit).**
`POST /pvm/api/0/station/setting/cfg_g_m` with `{sid, type: 515}` returns
`status: "0"` but `data: {}` on this station; the matching write is
`POST /pvm/api/0/station/setting/config` with
`{sid, type: 515, data: {max_power, power_a, power_b, power_c}}` in watts. It
targets hybrid/battery stations. See `Phil-0805/hoymiles-home-assistant`,
`docs/output-power-settings.md`, for the drawer implementation.

**Device settings actions 84 / 1532 (running output setting).**
`POST /pvm-ctl/api/0/dev/setting/read` with `{action: 84, data: {sid}}` never
leaves `code: 2` (20 polls, `speed[].rate: -1`), and action 1532 answers with
`data: null`. Both address a station controller device, which a microinverter
station does not have. The phase endpoint `station/phase/type_s_c` returns `[]`
(single phase), which is what selects 84 over 1532 in the web client.

**Per-device action 2006 (Power control drawer).**
`{action: 2006, dev_sn, dtu_sn, data: {mode: 1}}` also stays at `code: 2`
forever on this station. Both paths are correct for devices behind an LCM; they
are inert here.

## Persistence and wear

Action 8 changes a stored device setting: the value survives a restart, and the
`speed[].module`/`cfg_data.tag` fields show it is applied as a configuration
change, not a runtime override. There is no temporary/RAM variant on the `-WB`
series, so this is a flash write on every call. Occasional changes (a seasonal
setting, a manual adjustment) are fine; a control loop that writes every few
seconds is not.

The integration therefore exposes one `number` entity and no automatic loop:
`HoymilesStationPowerLimit` writes on demand and holds the requested value until
the station readback catches up.

## Verification log

| Step | Result |
|---|---|
| Read `config.power_limit` | `"50"` |
| Write 50 (same value) | `code: 0`, `rate: 100`, readback `"50"` |
| Write 52 | `code: 0`, `rate: 100`, readback `"52"` |
| Write 50 (restore) | `code: 0`, `rate: 100`, readback `"50"` |

The 52/50 pair proves the readback tracks the write; the earlier same-value write
alone could not have distinguished "applied" from "accepted and ignored".
