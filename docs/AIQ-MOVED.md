# AI-Q on AgentCore has moved

The AI-Q on Amazon Bedrock AgentCore module that lived under `aiq/` (runtime adapter, Open WebUI pipe and Action function,
Research Workbench, Model Lab, CDK stack) is now its own repository:

**https://github.com/AWS-fpenland/aiq-on-agentcore**

It treats this Open WebUI deployment as a prerequisite (Cognito user pool, app client ids, Open WebUI URL and an admin token to
install the pipe) and never modifies it. Its history, including the commits that were made here, was carried over with
`git filter-repo`. The decision records under `docs/solutions/` that were written during that work stay here as well.
