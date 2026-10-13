# Shubh Vivah: digital wedding invitation

An interactive Indian wedding invitation in a single HTML file, with no build step.

- Palace doors that open with a temple bell and a shower of marigold petals
- A scratch card that reveals the wedding date, then a live countdown and an "Add to Google Calendar" button
- An animated baraat: the groom rides in on a ghodi to a dhol beat, then the varmala exchange
- Event cards (Haldi, Mehendi, Sangeet, Baraat, Pheras, Reception) that colour and sort themselves
- The venue with Google Maps directions, plus a WhatsApp RSVP button
- Upload the couple's cartoon PNGs. They appear under the arch, on the horse, at the map pin, in the footer and as pop-ups.

## Files

| File | Use |
| --- | --- |
| `invite.html` | Page content as published to claude.ai. The platform adds the document skeleton. |
| `index.html` | The same page as a complete standalone document, for any static host (GitHub Pages, Netlify). |

## Editing

**On claude.ai**: anyone with edit access sees a **Customize invitation** button. Changes preview live. **Save and publish** writes the names, dates and photos into the page, so every guest sees them.

**Standalone**: open `index.html#customize`, make your edits and click **Download updated page**. Then upload the downloaded `index.html` to your host. In standalone mode the venue section shows a live embedded Google Map.

All times are India Standard Time.
