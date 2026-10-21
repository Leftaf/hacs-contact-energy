# NZ Contact Energy – Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/Leftaf/hacs-contact-energy)](https://github.com/Leftaf/hacs-contact-energy/releases)

Custom HACS integration that pulls **hourly and daily electricity consumption** from your [Contact Energy](https://www.contact.co.nz) (New Zealand) account and feeds it into the Home Assistant Energy Dashboard.

---

## Features

- **Energy Dashboard ready** – imports usage as external statistics so your consumption appears automatically in the Energy dashboard
- **Historical import** – on first setup, back-fills data from a configurable start date
- **Lag-aware polling** – Contact data often arrives with a 1–2 day delay; the integration re-fetches a configurable window of recent days
- **Repair service** – re-import any past date range when data is missing (portal offline, maintenance, failed poll, …)
- **Config Flow** – fully UI-configurable (no YAML required)
- **Options** – change refresh interval and lag window without re-adding the integration

---

## Installation

### HACS (recommended)

1. Open HACS → **Integrations**
2. Click the three dots (⋮) in the top right → **Custom repositories**
3. Add the repository URL and set category to **Integration**:

   ```
   https://github.com/Leftaf/hacs-contact-energy
   ```

4. Click **Add**
5. Search for **NZ Contact Energy** and download it
6. Restart Home Assistant

**One-click HACS link:**

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Leftaf&repository=hacs-contact-energy&category=integration)

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

After setup you can change the lag window and refresh interval via the integration’s **Configure** options.

---

## Entities & Statistics

| Name | Type | Description |
|------|------|-------------|
| `sensor.nz_contact_energy_consumption` | Sensor (Energy / Total Increasing) | Current total consumption sensor |
| External statistics | `nz_contact_energy:energy_consumption` | Hourly statistics for the Energy Dashboard |
| External statistics | `nz_contact_energy:free_energy_consumption` | Free / controlled-period energy (when available) |

The integration uses Contact’s unofficial API. Data is typically available with a 1–2 day lag.

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
- No cost / tariff data is imported (only kWh consumption)
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
