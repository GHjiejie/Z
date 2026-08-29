from __future__ import annotations


def create_oss_credentials_provider(oss_module):
    """Use Alibaba Cloud's default credential chain when available.

    The chain supports ECS/ACK RAM roles, OIDC, STS, and environment-backed
    development credentials without persisting a secret in platform state.
    """

    try:
        from alibabacloud_credentials.client import Client as CredentialsClient

        credentials_client = CredentialsClient()

        def resolve():
            credential = credentials_client.get_credential()
            return oss_module.credentials.Credentials(
                access_key_id=credential.access_key_id,
                access_key_secret=credential.access_key_secret,
                security_token=getattr(credential, "security_token", None),
            )

        return oss_module.credentials.CredentialsProviderFunc(func=resolve)
    except ImportError:
        return oss_module.credentials.EnvironmentVariableCredentialsProvider()
