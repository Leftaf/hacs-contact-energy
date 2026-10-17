# NZ Contact Energy – Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/Leftaf/hacs-contact-energy)](https://github.com/Leftaf/hacs-contact-energy/releases)

Custom HACS integration that pulls **hourly and daily electricity consumption** from your [Contact Energy](https://www.contact.co.nz) (New Zealand) account and feeds it into the Home Assistant Energy Dashboard.

---

## Features

- **Energy Dashboard ready** – imports usage as external statistics so your consumption appears automatically in the Energy dashboard
- **Historical import** – on first setup, back-fills data from a configurable start date
- **Lag-aware polling** – Contact data often arrives with a 1–2 day delay; the integration re-fetches a configurable window of recent days
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
| External statistics | `kWh` | Hourly + daily statistics pushed to the recorder for the Energy Dashboard |

The integration uses Contact’s unofficial API. Data is typically available with a 1–2 day lag.

---

## Notes & Limitations

- Requires a valid Contact Energy account (email + password)
- Uses an unofficial API – Contact may change or rate-limit it at any time
- No cost / tariff data is imported (only kWh consumption)
- Free / controlled-period energy (if present on your plan) is recorded separately when available

---

## Support

- Issues & feature requests: [GitHub Issues](https://github.com/Leftaf/hacs-contact-energy/issues)
- Documentation: this README

---

## License

MIT
