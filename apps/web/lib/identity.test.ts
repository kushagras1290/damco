import { authProviderOf, backendSubjectOf, identityFor, isIdentityProvider } from "@/lib/identity";

describe("identityFor", () => {
  it.each([
    ["github", "583231", "github:583231"],
    ["google", "110248495921238986420", "google:110248495921238986420"],
    ["microsoft-entra-id", "AAAAAAAAAAAAAAAAAAAAAIkzqFVrSaSaFHy782bbtaQ", "microsoft:AAAAAAAAAAAAAAAAAAAAAIkzqFVrSaSaFHy782bbtaQ"],
    ["email-link", "a".repeat(64), `email:${"a".repeat(64)}`],
  ])("maps %s accounts to immutable API subjects", (provider, id, subject) => {
    const identity = identityFor(provider, id);
    expect(identity && backendSubjectOf(identity)).toBe(subject);
  });

  it.each([
    ["github", "octocat"], // logins are mutable: numeric ids only
    ["email-link", "user@example.com"], // never raw addresses
    ["twitter", "123"], // unsupported provider
    ["google", "x:y"],
    ["google", undefined],
  ])("rejects %s / %s", (provider, id) => {
    expect(identityFor(provider, id)).toBeNull();
  });

  it("round-trips provider names", () => {
    expect(authProviderOf("microsoft")).toBe("microsoft-entra-id");
    expect(isIdentityProvider("email")).toBe(true);
    expect(isIdentityProvider("admin")).toBe(false);
  });
});
