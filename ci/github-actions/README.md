# GitHub Actions workflow (not enabled)

`ci.yml` is the continuous-integration workflow for this repository. It is kept here instead of in
`.github/workflows/` so that it does not run yet: it has never been executed, and pushing workflow
files needs a GitHub token with the `workflow` scope.

To enable it, move the file back and push with a token that has that scope:

```sh
git mv ci/github-actions/ci.yml .github/workflows/ci.yml
git commit -m "Enable the CI workflow"
git push
```

Expect to fix the first runs, because no step of this workflow has been run on GitHub yet.
