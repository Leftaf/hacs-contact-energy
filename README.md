# NZ Contact Energy – Home Assistant Integration

[![GitHub release](https://img.shields.io/github/v/release/Leftaf/hacs-contact-energy)](https://github.com/Leftaf/hacs-contact-energy/releases)
[![Marketplace](https://img.shields.io/badge/Home%20Assistant-Marketplace-blue.svg)](https://www.home-assistant.io/blog/2026/11/04/release-202611/)

Custom integration for **New Zealand** that pulls **hourly electricity consumption** from your [Contact Energy](https://www.contact.co.nz) account and feeds it into the Home Assistant Energy Dashboard.

Listed for country **NZ** in Marketplace / HACS metadata (`hacs.json` → `"country": "NZ"`).

---

## Features

- **Energy Dashboard ready** – imports usage as **external statistics** and onto the **consumption entity** so Energy and Statistics graph cards both work
- **Cost tracking** – hourly NZD cost from Contact’s `dollarValue`, exposed as a monetary total sensor for Energy cost tracking
- **Last API success** – timestamp sensor on the device (useful for Activity / last successful fetch)
- **Historical import** – on first setup, back-fills data from a configurable start date
- **Lag-aware polling** – Contact data often arrives with a 1–2 day delay; the integration re-fetches a configurable window of recent days
- **Repair service** – re-import any past date range when data is missing (portal offline, maintenance, failed poll, …)
- **Config Flow** – fully UI-configurable (no YAML required)
- **Options** – change refresh interval and lag window without re-adding the integration

---

## Installation

### Marketplace (Home Assistant 2026.11+, recommended)

1. Go to **Settings → Marketplace**
2. Choose one of:
   - **Search** for **NZ Contact Energy** (once the repository is in the default catalog), or
   - **Add from link** and enter:

     ```
     https://github.com/Leftaf/hacs-contact-energy
     ```
3. Install the integration
4. Restart Home Assistant if prompted (updates still require a restart)

**One-click Marketplace link:**

[![Open your Home Assistant instance and open a repository in the Marketplace.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=Leftaf&repository=hacs-contact-energy&category=integration)

The older [HACS repository link](https://my.home-assistant.io/redirect/hacs_repository/?owner=Leftaf&repository=hacs-contact-energy&category=integration) still works on HA 2026.11+ (it opens Marketplace) and on older versions that still use HACS.

### Manual

1. Copy the `nz_contact_energy` folder into `config/custom_components/`
2. Restart Home Assistant

---

## Configuration

1. Go to **Settings → Devices & services → Add Integration**
2. Search for **NZ Contact Energy**
3. Enter:

   | Field | Description | Default |
   |-------|-------------|---------|
   | **Email / Username** | Contact Energy account email | – |
   | **Password** | Your account password | – |
   | **First day of historical import** | Earliest date to pull (YYYY-MM-DD) | First day of the month, 2 months ago |
   | **Days to re-fetch (lag window)** | How many recent days to re-request on each update | `2` |
   | **Refresh Interval (hours)** | How often to poll the API | `24` |
| **Refresh start time** | Local time for the first daily poll | `03:00:00` |

After setup you can change the lag window, refresh interval, and **refresh start time** via the integration’s **Configure** options.

---

## Entities & Statistics

| Name | Type | Description |
|------|------|-------------|
| Consumption sensor | Energy / Total Increasing (kWh) | Running total; entity id may include your device name |
| Cost sensor | Monetary / Total (NZD, **excl. GST**) | Running total from hourly `dollarValue` |
| Last API success | Timestamp | Time of last successful Contact fetch (optional to hide) |
| External statistics | `nz_contact_energy:energy_consumption` | Hourly kWh (external) |
| External statistics | `nz_contact_energy:free_energy_consumption` | Free / controlled-period energy (when available) |
| Entity LTS | Same hours on the consumption & cost **entities** | Used when Energy / Statistics graph select the sensor |

Pick **either** the consumption **entity** **or** the external statistic as the Energy grid source — not both (would double-count).

The integration uses Contact’s unofficial API. Data is typically available with a 1–2 day lag.

### Upgrading to 1.1.0+

From 1.1.0, each successful import writes hourly kWh to the **entity** as well as external statistics. After upgrading:

1. Restart Home Assistant and check the device shows **1.1.0**.
2. Re-import history once if Statistics graph / Energy shows missing kWh on the sensor:

```yaml
action: nz_contact_energy.import_usage
data:
  start_date: "YYYY-MM-DD"   # your first_day_history
```

New lag-window polls then keep entity statistics updated automatically.

### Cost tracking (Energy dashboard)

Contact returns a **cost per hour** (`dollarValue` in NZD). The integration stores those values **per hour** (so peak / off-peak / night tariffs stay correct) and exposes a cumulative total on `sensor.nz_contact_energy_cost` (**excl. GST**, matching Contact bill lines before GST).

**Setup:**

1. Open **Settings → Dashboards → Energy → Grid consumption → Configure**
2. Under **Cost tracking**, choose **Use an entity tracking the total costs**
3. Select **`sensor.nz_contact_energy_cost`**

**Notes:**

- Cost is stored as **hourly statistics** with the **same timestamps** as kWh (so Energy days line up)
- Re-importing the same hour replaces that hour’s cost (no double-counting)
- After a full history import (or `import_usage` from your start date), the cost sensor total matches the sum of all stored hours
- If cost shows on the wrong day (or NZ$0.00 while kWh is fine), re-import that range once so cost statistics are rewritten:

```yaml
action: nz_contact_energy.import_usage
data:
  start_date: "2026-10-23"
  end_date: "2026-10-24"
```

---

## Repairing missing or incorrect history

Contact’s portal can be offline for maintenance, the API can fail temporarily, or a poll can miss a day.
When that happens the Energy Dashboard shows a gap or a large negative/positive spike.

Use the built-in **import_usage** service to re-fetch and rewrite any past range.

### How to call it

**Developer Tools → Actions**

```yaml
action: nz_contact_energy.import_usage
data:
  start_date: "2026-10-12"
  end_date: "2026-10-19"      # optional
```

| Field | Required | Description |
|-------|----------|-------------|
| `start_date` | Yes | First day to import (`YYYY-MM-DD`) |
| `end_date` | No | Last day to import. If omitted, the import automatically continues through **yesterday** so the running sum stays continuous. |

### Important: keep the running sum continuous

Statistics are stored as a **cumulative sum**. The Energy Dashboard shows the *change* between consecutive hours.

If you rewrite only day **N**, day **N+1** still has the old sum → a large spike appears at the boundary.

**Rules of thumb:**

1. **Prefer a range that ends at yesterday**
   Leave `end_date` empty, or set it to the latest day you care about. The service will rewrite everything from `start_date` forward so no boundary is left broken.

2. **Single-day repair**
   ```yaml
   action: nz_contact_energy.import_usage
   data:
     start_date: "2026-10-12"
     # no end_date → auto-extends through yesterday
   ```

3. **Portal was down for several days**
   ```yaml
   action: nz_contact_energy.import_usage
   data:
     start_date: "2026-10-10"
     end_date: "2026-10-15"
   ```
   After this, also re-import from `2026-10-16` through yesterday (or leave `end_date` empty on a second call starting at `2026-10-16`) so later days stay continuous.

4. **Check the log**
   After the action you should see lines like:
   ```
   Seed for nz_contact_energy:energy_consumption before 2026-10-12 = 1234.567
   Manual import finished: 2026-10-12 → 2026-10-21
   ```
   A seed of `0.0` means there was no prior statistic before that date (first import of that period).

### Example scenarios

| Situation | Action |
|-----------|--------|
| Portal offline 10–12 Oct | `start_date: "2026-10-10"`, leave `end_date` empty |
| One missing day (15 Oct) | `start_date: "2026-10-15"` (auto-extends) |
| Negative spike on a day | Re-import from that day through yesterday |
| Full history looks wrong | Re-import from your original `first_day_history` through yesterday |

Re-importing the same range is safe — existing statistics for those hours are replaced.

---

## Notes & Limitations

- Requires a valid Contact Energy account (email + password)
- Uses an unofficial API – Contact may change or rate-limit it at any time
- **Multiple contracts on one account are not tested.** The integration uses the first account and prefers a contract with `status: ACTIVE` (otherwise the first contract). If you have several active contracts (e.g. electricity + gas), behaviour is untested — please report samples via GitHub Issues
- Cost is imported from hourly `dollarValue` (NZD, **excl. GST**) via `sensor.nz_contact_energy_cost`
- Per-hour cost storage is ready for time-of-use tariffs (peak / off-peak / night)
- Free / controlled-period energy (if present on your plan) is recorded separately when available
- Contact data often lags by 1–2 days; the lag window setting covers normal delays
- Long date ranges are fetched day-by-day with a short pause to avoid rate limiting

---

## Support

- Issues & feature requests: [GitHub Issues](https://github.com/Leftaf/hacs-contact-energy/issues)
- Documentation: this README

---

## License

MIT
