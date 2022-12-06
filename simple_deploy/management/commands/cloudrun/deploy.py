"""Manages all Cloud Run-specific aspects of the deployment process."""


import sys, os, re, subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import CommandError
from django.utils.crypto import get_random_string
from django.utils.safestring import mark_safe

from simple_deploy.management.commands import deploy_messages as d_msgs
from simple_deploy.management.commands.cloudrun import deploy_messages as cloudrun_msgs

from simple_deploy.management.commands.utils import write_file_from_template

# TODO(glasnt): consider if a variation of self.sd.project_name could be used
# (https://github.com/ehmatthes/django-simple-deploy/issues/193#issuecomment-1338646565)
CLOUD_RUN_SERVICE_NAME = "django"

# TODO(glasnt): use self.sd.region  
CLOUD_RUN_REGION = "us-central1"

class PlatformDeployer:
    """Perform the initial deployment of a simple project.
    Configure as much as possible automatically.
    """

    def __init__(self, command):
        """Establishes connection to existing simple_deploy command object."""
        self.sd = command
        self.stdout = self.sd.stdout


    def deploy(self, *args, **options):
        self.sd.write_output("Configuring project for deployment to Cloud Run...")

        self._get_googlecloud_project()
        self._enable_apis()
        self._create_placeholder()
        self._set_on_cloudrun()

        self._generate_procfile()
        self._add_gcloudignore()
        self._add_cloudbuild_yaml()
        self._modify_settings()

        self._add_gunicorn()
        self._add_psycopg2_binary()
        self._add_dj_database_url()
        self._add_whitenoise()

        self._conclude_automate_all()

        self._show_success_message()

    def _enable_apis(self):
        """Before any other work can begin, a number of APIs must be enabled on the project"""

        msg = "Enabling Google Cloud APIs..."
        self.sd.write_output(msg)

        cmd = f"""gcloud services enable \
            run.googleapis.com \
            iam.googleapis.com \
            compute.googleapis.com \
            sql-component.googleapis.com \
            sqladmin.googleapis.com \
            cloudbuild.googleapis.com \
            cloudresourcemanager.googleapis.com \
            secretmanager.googleapis.com"""
        self.sd.execute_subp_run(cmd)

        msg = "  APIs enabled."
        self.sd.write_output(msg)

        

    def _create_placeholder(self):
        """Within the context of Google Cloud, things can exist.
        But within the context of a Cloud Run service, the service has to exist
        because configurations can be made against it. 
        The image of the service doesn't matter, but the service has to exist first. 
        The way to do this is to create a service with an initial placeholder revision
        using the "hello" placeholder service. https://github.com/googlecloudplatform/cloud-run-hello
        """
        msg = "Creating placeholder service..."
        self.sd.write_output(msg)

        # First, check service doesn't already exist.
        cmd = f"gcloud run services describe {CLOUD_RUN_SERVICE_NAME}"
        output_obj = self.sd.execute_subp_run(cmd)
        return_code = output_obj.returncode
        if return_code == 0:
            msg = "  Found placeholder service"
            self.sd.write_output(msg)
            return
        
        # Create placeholder service
        # Set visibility at this stage, and it should be always publicly accessible.
        cmd = f"gcloud run deploy django --image gcr.io/cloudrun/hello --allow-unauthenticated"
        output_obj = self.sd.execute_subp_run(cmd)
        output_str = output_obj.stdout.decode()
        self.sd.write_output(output_str)

        msg = "  Placeholder service created."
        self.sd.write_output(msg)



    def _set_on_cloudrun(self):
        """Set an environment variable, ON_CLOUDRUN. This is used in settings.py to apply
        deployment-specific settings.
        Returns:
        - None
        """
        msg = "Setting ON_CLOUDRUN envvar..."
        self.sd.write_output(msg)

        # First check if envvar has already been set.
        cmd = f"""gcloud run services describe {CLOUD_RUN_SERVICE_NAME} \
                    --format \"value(spec.template.spec.containers[0].env)\""""
        output_obj = self.sd.execute_subp_run(cmd)
        output_str = output_obj.stdout.decode()
        if 'ON_CLOUDRUN' in output_str:
            msg = "  Found ON_CLOUDRUN in existing envvars."
            self.sd.write_output(msg)
            return

        cmd = f"""gcloud run services update {CLOUD_RUN_SERVICE_NAME} \
                --set-env-vars ON_CLOUDRUN=1"""
        output_obj = self.sd.execute_subp_run(cmd)
        output_str = output_obj.stdout.decode()
        self.sd.write_output(output_str)

        msg = "  Set ON_CLOUDRUN envvar."
        self.sd.write_output(msg)


    def _generate_procfile(self):
        """Create Procfile, if none present."""

        #   Procfile should be in project root, if present.
        self.sd.write_output(f"\n  Looking in {self.sd.git_path} for Procfile...")
        procfile_present = 'Procfile' in os.listdir(self.sd.git_path)

        if procfile_present:
            self.sd.write_output("    Found existing Procfile.")
        else:
            self.sd.write_output("    No Procfile found. Generating Procfile...")
            if self.sd.nested_project:
                proc_command = f"web: gunicorn {self.sd.project_name}.{self.sd.project_name}.wsgi --log-file -"
            else:
                proc_command = f"web: gunicorn {self.sd.project_name}.wsgi --log-file -"

            migrate_command = "migrate: python manage.py migrate && python manage.py collectstatic --noinput"

            with open(f"{self.sd.git_path}/Procfile", 'w') as f:
                f.write(proc_command)
                f.write(migrate_command)

            self.sd.write_output("    Generated Procfile with following process:")
            self.sd.write_output(f"      {proc_command}")
            self.sd.write_output(f"      {migrate_command}")
            


    def _add_gcloudignore(self):
        """Add a gcloudignore file, based on user's local project environmnet.
        Ignore virtual environment dir, system-specific cruft, and IDE cruft.

        Based on the dockerignore config from flyio

        If an existing gcloudignore is found, make note of that but don't overwrite.

        Returns:
        - True if added gcloudignore.
        - False if gcloudignore found unnecessary, or if an existing dockerfile
          was found.
        """

        # Check for existing gcloudignore file; we're only looking in project root.
        #   If we find one, don't make any changes.
        path = Path('.gcloudignore')
        if path.exists():
            msg = "  Found existing .gcloudignore file. Not overwriting this file."
            self.sd.write_output(msg)
            return

        # Build gcloudignore string.
        gcloudignore_str = ""

        # Ignore git repository.
        gcloudignore_str += ".git/\n"

        # Ignore venv dir if a venv is active.
        venv_dir = os.environ.get("VIRTUAL_ENV")
        if venv_dir:
            venv_path = Path(venv_dir)
            gcloudignore_str += f"\n{venv_path.name}/\n"


        # Add python cruft.
        gcloudignore_str += "\n__pycache__/\n*.pyc\n"

        # Ignore any SQLite databases.
        gcloudignore_str += "\n*.sqlite3\n"

        # If on macOS, add .DS_Store.
        if self.sd.on_macos:
            gcloudignore_str += "\n.DS_Store\n"

        # Write file.
        path.write_text(gcloudignore_str)
        msg = "  Wrote .gcloudignore file."
        self.sd.write_output(msg)


    def _add_cloudbuild_yaml(self):
        """Add a cloudbuild.yaml file."""
        # File should be in project root, if present.
        self.sd.write_output(f"\n  Looking in {self.sd.git_path} for cloudbuild.yaml file...")
        cloudbuildyaml_present = 'cloudbuild.yaml' in os.listdir(self.sd.git_path)

        if cloudbuildyaml_present:
            self.sd.write_output("    Found existing cloudbuild.yaml file.")
        else:
            # Generate file from template.
            context = {
                'service_name': CLOUD_RUN_SERVICE_NAME, 
                }
            path = self.sd.project_root / 'cloudbuild.yaml'
            write_file_from_template(path, 'cloudbuild.yaml', context)

            msg = f"\n    Generated cloudbuild.yaml: {path}"
            self.sd.write_output(msg)
            return path


    def _modify_settings(self):
        """Add settings specific to Cloud Run."""
        #   Check if a cloudrun section is present. If not, add settings. If already present,
        #   do nothing.
        self.sd.write_output("\n  Checking if settings block for Cloud Run present in settings.py...")

        with open(self.sd.settings_path) as f:
            settings_string = f.read()

        if 'if os.environ.get("ON_CLOUDRUN"):' in settings_string:
            self.sd.write_output("\n    Found Cloud Run settings block in settings.py.")
            return

        # Add Cloud Run settings block.
        self.sd.write_output("    No Cloud Run settings found in settings.py; adding settings...")

        safe_settings_string = mark_safe(settings_string)
        context = {
            'current_settings': safe_settings_string,
            'deployed_project_name': self.deployed_project_name,
        }
        path = Path(self.sd.settings_path)
        write_file_from_template(path, 'settings.py', context)

        msg = f"    Modified settings.py file: {path}"
        self.sd.write_output(msg)


    def _add_gunicorn(self):
        """Add gunicorn to project requirements."""
        self.sd.write_output("\n  Looking for gunicorn...")

        if self.sd.using_req_txt:
            self.sd.add_req_txt_pkg('gunicorn')
        elif self.sd.using_pipenv:
            self.sd.add_pipenv_pkg('gunicorn')


    def _add_psycopg2_binary(self):
        """Add psycopg2-binary to project requirements."""
        self.sd.write_output("\n  Looking for psycopg2-binary...")

        if self.sd.using_req_txt:
            self.sd.add_req_txt_pkg('psycopg2-binary')
        elif self.sd.using_pipenv:
            self.sd.add_pipenv_pkg('psycopg2-binary')

    def _add_dj_database_url(self):
        """Add dj-database-url to project requirements."""
        self.sd.write_output("\n  Looking for dj-database-url...")

        if self.sd.using_req_txt:
            self.sd.add_req_txt_pkg('dj-database-url')
        elif self.sd.using_pipenv:
            self.sd.add_pipenv_pkg('dj-database-url')

    def _add_whitenoise(self):
        """Add whitenoise to project requirements."""
        self.sd.write_output("\n  Looking for whitenoise...")

        if self.sd.using_req_txt:
            self.sd.add_req_txt_pkg('whitenoise')
        elif self.sd.using_pipenv:
            self.sd.add_pipenv_pkg('whitenoise')


    def _conclude_automate_all(self):
        """Finish automating the push to Fly.io.
        - Commit all changes.
        - Call `fly deploy`.
        - Call `fly open`, and grab URL.
        """
        # Making this check here lets deploy() be cleaner.
        if not self.sd.automate_all:
            return

        self.sd.commit_changes()

        # Push project.
        # Use execute_command() to stream output of this long-running command.
        self.sd.write_output("  Deploying to Cloud Run...")
        cmd = "gcloud builds submit"
        self.sd.execute_command(cmd)

        # Open project.
        self.sd.write_output("  Opening deployed app in a new browser tab...")
        cmd = "gcloud run services describe django --format \"value(status.url)\""
        output = self.sd.execute_subp_run(cmd)
        self.sd.write_output(output)
        self.deployed_url = output


    def _show_success_message(self):
        """After a successful run, show a message about what to do next."""

        # DEV:
        # - Mention that this script should not need to be run again, unless
        #   creating a new deployment.
        #   - Describe ongoing approach of commit, push, migrate. Lots to consider
        #     when doing this on production app with users, make sure you learn.

        if self.sd.automate_all:
            msg = cloudrun_msgs.success_msg_automate_all(self.deployed_url)
            self.sd.write_output(msg)
        else:
            msg = cloudrun_msgs.success_msg(log_output=self.sd.log_output)
            self.sd.write_output(msg)


    # --- Methods called from simple_deploy.py ---

    def confirm_preliminary(self):
        """Deployment to Fly.io is in a preliminary state, and we need to be
        explicit about that.
        """
        # Skip this confirmation when unit testing.
        if self.sd.unit_testing:
            return

        self.stdout.write(cloudrun_msgs.confirm_preliminary)
        confirmed = self.sd.get_confirmation(skip_logging=True)

        if confirmed:
            self.stdout.write("  Continuing with Cloud Run deployment...")
        else:
            # Quit and invite the user to try another platform.
            # We are happily exiting the script; there's no need to raise a
            #   CommandError.
            self.stdout.write(cloudrun_msgs.cancel_cloudrun)
            sys.exit()


    def validate_platform(self):
        """Make sure the local environment and project supports deployment to
        Cloud Run.
        
        The returncode for a successful command is 0, so anything truthy means
          a command errored out.
        """
        self._validate_cli()

        # When running unit tests, will not be logged into CLI.
        if not self.sd.unit_testing:

            self.deployed_project_name = self._get_deployed_project_name()

            # Create the db now, before any additional configuration. Get region
            #   so we know where to create the db.
            self.region = self._get_region()
            self._create_db()
        else:
            self.deployed_project_name = self.sd.deployed_project_name


    def prep_automate_all(self):
        """Take any further actions needed if using automate_all."""
        # All creation has been taken earlier, during validation.
        pass

    
    def _get_googlecloud_project(self):
        """Use the gcloud CLI to get the current active project"""
        msg = "Finding active Google Cloud project"

        cmd = f"gcloud config get-value project"
        output_obj = self.sd.execute_subp_run(cmd)
        project_id = output_obj.stdout.decode()
        if project_id == "(unset)": 
            raise CommandError(cloudrun_msgs.no_project_id)
        msg = f"  Found Google Cloud project: {project_id}"
        self.sd.write_output(msg)
        self.project_id = project_id

    def _get_googlerun_region(self):
        """Use the gcloud CLI to get the Cloud Run region"""
        msg = "Finding configured Cloud Run region"

        cmd = f"gcloud config get-value run/region"
        output_obj = self.sd.execute_subp_run(cmd)
        region = output_obj.stdout.decode()
        if region == "(unset)": 
            raise CommandError(cloudrun_msgs.no_cloudrun_region)
        msg = f"  Found Cloud Run region: {region}"
        self.sd.write_output(msg)
        return region

    def _get_random_string(length=20):
        """Get a random string, for secret keys and database passwords"""
        return get_random_string(length,
                    allowed_chars='abcdefghijklmnopqrstuvwxyz0123456789')

    # --- Helper methods for methods called from simple_deploy.py ---

    def _validate_cli(self):
        """Make sure the Google Cloud CLI is installed."""
        cmd = 'gcloud version'
        output_obj = self.sd.execute_subp_run(cmd)
        if output_obj.returncode:
            raise CommandError(cloudrun_msgs.cli_not_installed)


    def _create_db(self):
        """Create a postgres instance, database, user, and secret.

        Method will use an existing instance if it exists, with prompt.
        
        Returns: 
        - 
        - Raises CommandError if...
        """

        self.instance_name = f"{CLOUD_RUN_SERVICE_NAME}-instance"
        self.database_name = "django-db" #TODO(glasnt) change?
        self.database_user = "django-user"
        self.database_pass = get_random_string()
        self.instance_pass = get_random_string()

        msg = "Looking for a Postgres instance..."
        self.sd.write_output(msg, skip_logging=True)

        instance_exists = self._check_if_dbinstance_exists()

        if instance_exists:
            msg = "  Found existing instance."
            self.sd.write_output(msg, skip_logging=True)
        else:
            msg = f"  Create a new Postgres database..."
            self.sd.write_output(msg, skip_logging=True)

            # TODO(glasnt) instance size?
            cmd = f"""gcloud sql instances create {self.instance_name} \
                        --database-version POSTGRES_14 --cpu 1 --memory 2GB  \
                        --region {self.region} \
                        --project {self.project_id} \
                        --root-password {self.instance_pass} \
                        --async --format="value(name)""
                """

            # If not using automate_all, make sure it's okay to create a resource
            #   on user's account.
            if not self.sd.automate_all:
                self._confirm_create_instance(db_cmd=cmd)

            # Create database.
            # Use execute_command(), to stream output of long-running process.
            self.sd.execute_command(cmd, skip_logging=True)

            msg = "  Created Postgres instance"
            self.sd.write_output(msg, skip_logging=True)

        db_exists = self._check_if_db_exists()
        if db_exists:
            msg = "  Database exists, using that one."
            self.sd.write_output(msg, skip_logging=True)
        else:
            msg = "  Creating new database..."
            self.sd.write_output(msg, skip_logging=True)
            cmd = f"""gcloud sql databases create {self.database_name} \
                        --instance {self.instance_name}
                """
            output_obj = self.sd.execute_subp_run(cmd)
            msg = "  Created Postgres database"
            self.sd.write_output(msg, skip_logging=True)
        

        user_exists = self._check_if_dbuser_exists()
        secret_exists = self._check_if_dbsecret_exist()
        if user_exists and secret_exists:
            msg = "  Database user and secret exists."
            self.sd.write_output(msg)
        elif user_exists and not secret_exists:
            msg = "  Database user exists, but password not stored."
        elif not user_exists and not secret_exists:
            msg = "  Database user doesn't exist. Creating."

        #TODO(glasnt): create user
        #TODO(glasnt): create secret

        msg = "  Created secret"
        self.sd.write_output(msg, skip_logging=True)

    def _check_if_dbinstance_exists(self):
        """Check if a postgres instance already exists that should be used with this app.
        Returns:
        - True if db found.
        - False if not found.
        """

        # First, see if any Postgres instances exist.
        cmd = "gcloud sql instances list"
        output_obj = self.sd.execute_subp_run(cmd)
        output_str = output_obj.stdout.decode()

        if "Listed 0 items" in output_str:
            msg = "  No Postgres instance found."
            self.sd.write_output(msg, skip_logging=True)
            return False
        elif self.instance_name in output_str:
            msg = "  Postgres instance was found."
            self.sd.write_output(msg, skip_logging=True)
            return True
        else:
            msg = "  A Postgres instance was found, but not what we expected."
            self.sd.write_output(msg, skip_logging=True)
            return False

    def _confirm_create_instance(self, db_cmd):
        """We really need to confirm that the user wants a instance created on their behalf.
        Show the command that will be run on the user's behalf.
        Returns:
        - True if confirmed.
        - Raises CommandError if not confirmed.
        """
        if self.sd.unit_testing:
            return

        self.stdout.write(cloudrun_msgs.confirm_create_instance(db_cmd))
        confirmed = self.sd.get_confirmation(skip_logging=True)

        if confirmed:
            self.stdout.write("  Creating instance...")
        else:
            # Quit and invite the user to create a database manually.
            raise CommandError(cloudrun_msgs.cancel_no_instance)

    def _check_if_db_exists(self):
        """Check if a postgres datbase already exists that should be used with this app.
        Returns:
        - True if db found.
        - False if not found.
        """

        # First, see if any Postgres instances exist.
        cmd = f"gcloud sql databases list --instance {self.instance_name}"
        output_obj = self.sd.execute_subp_run(cmd)
        output_str = output_obj.stdout.decode()

        if self.database_name in output_str:
            msg = f"  Database {self.database_name} found"
            self.sd.write_output(msg, skip_logging=True)
            return True
        else:
            msg = "  Database not found"
            self.sd.write_output(msg, skip_logging=True)
            return False
