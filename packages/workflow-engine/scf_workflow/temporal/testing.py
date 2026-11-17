"""The Temporal test server every test suite's time-skipping environment starts (#290)."""

#: Pinned, because the SDK's ``"default"`` follows the newest release: with sdk-java
#: v1.41.0 the engine parity test never returns. Upgrade it as its own change.
TEST_SERVER_VERSION = "v1.40.0"
