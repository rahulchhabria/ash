# Local Search

> Resolve local businesses and current opening hours through Google Places

Files: src/ash/integrations/search.py,
       src/ash/tools/builtin/google_places.py

## Requirements

### MUST

- Register `google_places` only when a Google Maps API key is configured.
- Execute requests in a dedicated one-use bridge-network sandbox.
- Keep the API key in the sandbox environment and out of command arguments/results.
- Use Places API (New) Text Search with an explicit field mask.
- Return the matched place name, address, business status, current or regular hours,
  phone number, website, and Google Maps URL when supplied by Google.
- Prefer `google_places` over general web search for named businesses, branch
  resolution, phone numbers, and opening-hours questions.
- Require latitude and longitude together when applying a location bias.
- Never route local-business lookup tasks to the Gmail/Calendar `google` skill.

### SHOULD

- Include city, neighborhood, address, or cross streets in text queries when known.
- Return multiple candidates when the requested branch is ambiguous.
- Cache identical lookups for 15 minutes.

## Configuration

```toml
[google_places]
api_key = "..." # or GOOGLE_MAPS_API_KEY
max_results = 5
```

The Places API key SHOULD be restricted to Places API (New) and to the deployment's
egress addresses where operationally possible.
