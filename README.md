# OpenShift 4 Utilities

```bash
./create_rhcos_ami.py --help                                                                                                                                                                  <aws:govcloud>
Usage: create_rhcos_ami.py [OPTIONS] [OCP_VERSIONS]...

  Create RHCOS AMIs for the given OCP_VERSIONS.

  Finds the RHCOS releses for the given OCP_VERSIONS and creates AMIs for each
  of them.

Options:
  --s3-bucket TEXT        Name of S3 bucket to upload disk images  [required]
  --public / --no-public  Set permissions on AMIs as public or private
  --help                  Show this message and exit.
```
