---
name: httpbin_echo
method: GET
url: https://httpbin.org/get
query:
  q: "{{query}}"
  n: "{{count}}"
params:
  query: {type: string, required: true, description: The text to echo back}
  count: {type: integer, description: How many times, purely illustrative}
---

Echoes a query string back via httpbin. Use this only when explicitly asked to
test the tool pipeline; it has no real-world use.
