# Darwin and Keewano in Claude Code (hackathon notes, outside Morph)

These notes are from the hackathon, for asking Darwin or Keewano something live from Claude Code, a separate tool. They
are not part of Morph: Morph never uses them, never needs a Darwin account and never pays for anything. Its Darwin
calls are Darwin's free public Search ([docs/SPONSORS.md](../SPONSORS.md)). Anything you set up here, including a
payment method with Darwin, is between you and Darwin.

Run in a terminal, not inside a Claude Code session (put your own Keewano personal access token in place of
`KEEWANO_PAT_VALUE`):

```shell
claude mcp add --scope user --transport http darwin https://mcp.darwin.so/mcp
claude mcp add --scope user --transport http keewano https://api.keewano.com/mcp --header "k-token: KEEWANO_PAT_VALUE"
```

- Darwin search works without signing in. Using a Darwin agent (Act) asks for a Darwin sign-in the first time, and
  Darwin's paid tools (such as its part lookup) charge your own Darwin account.
- Example asks: "use Darwin to find who sells the Feetech STS3215"; "in our Keewano project, which pad style needed the
  most extra holding force?"
