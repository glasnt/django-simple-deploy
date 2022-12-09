"""A collection of messages used in cloudrun/deploy.py."""

# For conventions, see documentation in deploy_messages.py

from textwrap import dedent

from django.conf import settings


confirm_preliminary = """
***** Deployments to Cloud Run are experimental at this point ***

- Support for deploying to Cloud Run is in an exploratory phase at this point.
- You should only be using this project to deploy to Cloud Run at this point if
  you are interested in helping to develop or test the simple_deploy project.
- You should look at the deploy/cloudrun.py script before running this command,
  so you know what kinds of changes will be made to your project.
- This command will likely fail if you run it more than once.
- This command may not work if you already have a project deployed to Cloud Run.
- You should understand the Google Cloud console, and be comfortable deleting resources
  that are created during this deployment.
- You may want to cancel this run and deploy to a different platform.
"""

confirm_automate_all = """
The --automate-all flag means simple_deploy will:
- Commit all changes to your project that are necessary for deployment, including:
  - Create a Cloud SQL instance, database, username, and associated secrets.
- Apply these changes to Google Cloud.
- Open your deployed project in a new browser tab.
"""

cancel_cloudrun = """
Okay, cancelling Cloud Run deployment.
"""

# DEV: Update URL
# DEV: This could be moved to deploy_messages, with an arg for platform and URL.
cli_not_installed = """
In order to deploy to Cloud Run, you need to install the Google Cloud CLI.
  See here: https://cloud.google.com/sdk/docs/install
After installing the CLI, you can run simple_deploy again.
"""

no_project_id = """
A Google Cloud project could not be found.

The simple_deploy command expects that you've already created a project
to deploy a Cloud Run service in.

If you haven't done so, create a new project with billing enabled: 

https://console.cloud.google.com/projectcreate

Then, configure your gcloud CLI for this project: 

    $ gcloud config set project PROJECT_ID

Then run simple_deploy again.
"""

# TODO(glasnt): remove.
no_cloudrun_region = """
A configuration for the Cloud Run region could not be found.

The simple_deploy command expects that you've already configured
the region to deploy your project to. 

The list of possible regions is available at: 

https://cloud.google.com/run/docs/locations

Commonly, the default region used is "us-central1". 

Configure the default Cloud Run region: 

    $ gcloud config set run/region REGION

Then run simple_deploy again.
"""


create_app_failed = """
Could not create a Cloud Run service.

TODO(glasnt): update text

The simple_deploy command can not proceed without a Cloud Run app to deploy to.
You may have better luck with a configuration-only run, if you can create a Cloud Run
app on your own. You may try the following command, and see if you can use any 
error messages to troubleshoot app creation:

    $ fly apps create --generate-name

If you can get this command to work, you can run simple_deploy again and the 
rest of the process may work.
"""

cancel_no_instance = """
A database instance is required for deployment. You may be able to create a database instance
manually, and configure it to work with this app.
"""

cancel_service_name = """
A service name could not be generated for you that would be valid in Cloud Run. 
To correct this, run simple-deploy again, specifying your preferred service name: 

  $ python manage.py simple_deploy --platform cloudrun --deployed_project_name YOURSERVICENAME
"""


# --- Dynamic strings ---
# These need to be generated in functions, to display information that's
#   determined as the script runs.


def confirm_use_org_name(org_name):
    """Confirm use of this org name to create a new project."""

    msg = dedent(
        f"""
        --- The Platform.sh CLI requires an organization name when creating a new project. ---
        When using --automate-all, a project will be created on your behalf. The following
        organization name was found: {org_name}

        This organization will be used to create a new project. If this is not okay,
        enter n to cancel this operation.
    """
    )

    return msg


def confirm_create_instance(db_cmd):
    """Confirm it's okay to create a Postgres instance on the user's account."""

    msg = dedent(
        f"""
        A Postgres instance is required to continue with deployment. If you confirm this,
        the following command will be run, to create a new instance on your account:
        $ {db_cmd}
    """
    )

    return msg


def success_msg(log_output=""):
    """Success message, for configuration-only run."""

    msg = dedent(
        f"""
        --- Your project is now configured for deployment on Cloud Run ---

        To deploy your project, you will need to:
        - Commit the changes made in the configuration process.
            $ git status
            $ git add .
            $ git commit -am "Configured project for deployment."
        - Push your project to Cloud Run's servers:
            $ gcloud builds submit
        - Open your project:
            $ gcloud run services list   
        - As you develop your project further:
            - Make local changes
            - Commit your local changes
            - Run `gcloud builds submit`
    """
    )

    if log_output:
        msg += dedent(
            f"""
        - You can find a full record of this configuration in the simple_deploy_logs directory.
        """
        )

    return msg


def success_msg_automate_all(deployed_url):
    """Success message, when using --automate-all."""

    msg = dedent(
        f"""

        --- Your project should now be deployed on Cloud Run ---

        It should have opened up in a new browser tab.
        - You can also visit your project at {deployed_url}

        If you make further changes and want to push them to Cloud Run,
        commit your changes and then run `fly deploy`.
    """
    )
    return msg


def confirm_service_name(project_name, service_name):
    msg = dedent(
        f"""

        Your Django project is called {project_name}, but this name
        is too complex for Cloud Run to process. 

        We want to call your Cloud Run service "{service_name}", instead.

        Please confirm this is okay. 
    """
    )
    return msg


def no_database_password(database_user, database_instance):
    msg = dedent(
        f"""
        A database user exists, but there's no secret storing it's password. 
        Based on this, we can't continue the process.

        You can resolve this by deleting the database user, and having simple_deploy re-create it: 

        $ gcloud sql users delete {database_user} --instance {database_instance}
    """
    )
    return msg
