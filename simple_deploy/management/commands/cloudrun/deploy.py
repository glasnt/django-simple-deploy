"""Manages all Cloud Run-specific aspects of the deployment process."""


import sys, os, re, subprocess
from pathlib import Path
from anyascii import anyascii
import tempfile
from django.conf import settings
from django.core.management.base import CommandError
from django.utils.crypto import get_random_string
from django.utils.safestring import mark_safe

from simple_deploy.management.commands import deploy_messages as d_msgs
from simple_deploy.management.commands.cloudrun import deploy_messages as cloudrun_msgs

from simple_deploy.management.commands.utils import write_file_from_template

# TODO(glasnt): use self.sd.region  
CLOUD_RUN_REGION = "us-central1"
ARTIFACT_REGISTRY = "containers"



class PlatformDeployer:
    """Perform the initial deployment of a simple project.
    Configure as much as possible automatically.
    """

    def __init__(self, command):
        """Establishes connection to existing simple_deploy command object."""
        self.sd = command
        self.stdout = self.sd.stdout


    # --- Helper commands ---
    def log(self, msg): 
        self.sd.write_output(msg)
    
    def run(self, cmd, stream=False, fail=False):
        print("🪵", re.sub(' +', ' ',cmd))
        if stream:
            self.sd.execute_command(cmd)
        else:
            output_obj = self.sd.execute_subp_run(cmd)
            return_code = output_obj.returncode
            return_str = output_obj.stdout.decode().strip()
            error_str = output_obj.stderr.decode().strip()
            if return_code == 0: 
                print("✅", return_code)
                print("🟢", return_str)
            else:
                print("❓", return_code)
                print("🔴", return_str)
                print("🟥", error_str)
                if fail:
                    sys.exit(1)
            return return_code, return_str

        
    def deploy(self, *args, **options):
        self.log("Configuring project for deployment to Cloud Run...")

        # Setup checking 
        self._get_googlecloud_project()
        self._get_googlerun_region()
        self._get_service_name()

        # Resource creation
        self._enable_apis()
        self._update_iam()
        self._create_placeholder()
        self._get_cloudrun_service_url()
        self._set_on_cloudrun()
        self._create_registry()
        self._create_db() # TODO(glasnt) move?

        # Configuration
        self._generate_procfile()
        self._add_gcloudignore()
        self._add_cloudbuild_yaml()
        self._modify_settings()
        self._add_python_packages()

        self._conclude_automate_all()
        self._show_success_message()

    def _enable_apis(self):
        """Before any other work can begin, a number of APIs must be enabled on the project"""
        self.log("Enabling Google Cloud APIs...")
        self.run(f"""gcloud services enable \
            run.googleapis.com \
            iam.googleapis.com \
            compute.googleapis.com \
            sql-component.googleapis.com \
            sqladmin.googleapis.com \
            cloudbuild.googleapis.com \
            artifactregistry.googleapis.com \
            cloudresourcemanager.googleapis.com \
            secretmanager.googleapis.com""")
        self.log("  APIs enabled.")
        

    def _update_iam(self):
        """There's some IAM configurations that will need to be changed."""

        self.log("Configuring IAM...")

        self.cloudrun_sa = f"{self.project_num}-compute@developer.gserviceaccount.com"
        self.cloudbuild_sa = f"{self.project_num}@cloudbuild.gserviceaccount.com"

        self.run(f"""gcloud iam service-accounts add-iam-policy-binding {self.cloudrun_sa} \
                --member "serviceAccount:{self.cloudbuild_sa}" \
                --role "roles/iam.serviceAccountUser"  """)

        self.run(f"""gcloud projects add-iam-policy-binding {self.project_id} \
                --member "serviceAccount:{self.cloudbuild_sa}" \
                --role "roles/run.developer"  """)

        self.log("  Updated IAM.")


    def _get_service_name(self):
        # Use the provided name if --deployed-project-name specified.
        if self.sd.deployed_project_name:
            project_name = self.sd.deployed_project_name
        else: 
            project_name = self.sd.project_name

        service_name = project_name
        """Cloud Run service names must be valid Kubernetes Object names.
        These differ from Django project names, which are python identifiers."""

        # cast underscores to hyphens
        if "_" in service_name:
            service_name = service_name.replace("_", "-")

        # convert any non-ascii characters to ascii. 
        ascii_service_name = anyascii(service_name)

        if ascii_service_name != service_name:
            # Service name changed dramatically. Confirm change 

            self.stdout.write(cloudrun_msgs.confirm_service_name(project_name, ascii_service_name))
            confirmed = self.sd.get_confirmation(skip_logging=True)

            if not confirmed:
                    self.log(cloudrun_msgs.cancel_service_name)
                    sys.exit()

        self.log(f"Django project: {project_name}. Cloud Run service: {service_name}")

        self.service_name = service_name



    def _create_placeholder(self):
        """Within the context of Google Cloud, things can exist.
        But within the context of a Cloud Run service, the service has to exist
        because configurations can be made against it. 
        The image of the service doesn't matter, but the service has to exist first. 
        The way to do this is to create a service with an initial placeholder revision
        using the "hello" placeholder service. https://github.com/googlecloudplatform/cloud-run-hello
        """
        self.log("Creating placeholder service...")

        # First, check service doesn't already exist.
        return_code, _ = self.run(f"gcloud run services describe {self.service_name} --region {self.region} ")
        if return_code == 0:
            self.log("  Found placeholder service")
            return
        
        # Create placeholder service
        # Set visibility at this stage, and it should be always publicly accessible.
        _, return_str = self.run(f"gcloud run deploy {self.service_name} --region {self.region} --image gcr.io/cloudrun/hello --allow-unauthenticated")
        self.log(return_str)
        self.log("  Placeholder service created.")

    def _get_cloudrun_service_url(self): 
        """Using the service name, get the cloud run service URL"""

        self.log("Getting Cloud Run service URL....")
        _, return_str =self.run(f"gcloud run services describe {self.service_name} --region {self.region}  --format \"value(status.url)\"")
        self.log(f"  {return_str}")
        self.deployed_url = return_str.strip()


    def _set_on_cloudrun(self):
        """Set an environment variable, ON_CLOUDRUN. This is used in settings.py to apply
        deployment-specific settings.
        """
        self.log("Setting ON_CLOUDRUN envvar...")

        # First check if envvar has already been set.
        _, return_str = self.run(f"""gcloud run services describe {self.service_name} --region {self.region} \
                    --format \"value(spec.template.spec.containers[0].env)\"""")

        if 'ON_CLOUDRUN' in return_str:
            self.log("  Found ON_CLOUDRUN in existing envvars.")
            return

        _, return_str = self.run(f"""gcloud run services update {self.service_name} --region {self.region}  \
                --set-env-vars ON_CLOUDRUN=1""")
        self.log("  Set ON_CLOUDRUN envvar.")


    def _create_registry(self):
        """Create an Artifact Registry for storing images"""
        self.log("Creating an Artifact Registry")

        _, return_str = self.run(f"gcloud artifacts repositories list --location {self.region}")

        if ARTIFACT_REGISTRY in return_str:
            self.log(" Artifact Registry found.")
            return

        self.run(f"gcloud artifacts repositories create {ARTIFACT_REGISTRY} --repository-format=docker --location {self.region}")

    def _create_container(self):
        self.registry_name = f"{self.region}-docker.pkg.dev/{self.project_id}/{ARTIFACT_REGISTRY}"
        self.image_name = f"{self.registry_name}/{self.service_name}"

        _, return_str = self.run(f"gcloud artifacts docker images list {self.registry_name}")
        if self.image_name in return_str:
            self.log("  Image exists.")
            return
        
        self.log("Creating container image...")
        self.run(f"gcloud builds submit --pack image={self.image_name}", stream=True)

    def _create_migrate_job(self): 
        self.log("Creating migration job definition...")
        self.migrate_job_name = "migrate"

        _, return_str = self.run(f"gcloud beta run jobs describe {self.migrate_job_name} --region {self.region}")
        if self.migrate_job_name in return_str:
            self.log(f"Cloud Run job {self.migrate_job_name} already exists.")
            return

        self.run(f"""gcloud beta run jobs create migrate \
            --image {self.image_name} \
            --region {self.region} \
            --set-secrets DATABASE_URL={self.database_secret}:latest \
            --set-cloudsql-instances {self.instance_fqn} \
            --command "migrate" """, fail=True)

    def _generate_procfile(self):
        """Create Procfile, if none present."""

        #   Procfile should be in project root, if present.
        self.log(f"\n  Looking in {self.sd.git_path} for Procfile...")

        procfile_present = 'Procfile' in os.listdir(self.sd.git_path)

        if procfile_present:
            self.log("    Found existing Procfile.")
        else:
            self.log("    No Procfile found. Generating Procfile...")
            if self.sd.nested_project:
                proc_command = f"web: gunicorn {self.sd.project_name}.{self.sd.project_name}.wsgi --log-file -"
            else:
                proc_command = f"web: gunicorn {self.sd.project_name}.wsgi --log-file -"

            migrate_command = "migrate: python manage.py migrate && python manage.py collectstatic --noinput"

            with open(f"{self.sd.git_path}/Procfile", 'w') as f:
                f.write(proc_command)
                f.write(migrate_command)

            self.log("    Generated Procfile with following process:")
            self.log(f"      {proc_command}")
            self.log(f"      {migrate_command}")
            


    def _add_gcloudignore(self):
        """Add a gcloudignore file, based on user's local project environmnet.
        Ignore virtual environment dir, system-specific cruft, and IDE cruft.

        Based on the dockerignore config from flyio

        If an existing gcloudignore is found, make note of that but don't overwrite.
        """

        # Check for existing gcloudignore file; we're only looking in project root.
        #   If we find one, don't make any changes.
        path = Path('.gcloudignore')
        if path.exists():
            self.log("  Found existing .gcloudignore file. Not overwriting this file.")
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
        self.log("  Wrote .gcloudignore file.")


    def _add_cloudbuild_yaml(self):
        """Add a cloudbuild.yaml file."""
        # File should be in project root, if present.
        self.log(f"\n  Looking in {self.sd.git_path} for cloudbuild.yaml file...")
        cloudbuildyaml_present = 'cloudbuild.yaml' in os.listdir(self.sd.git_path)

        if cloudbuildyaml_present:
            self.log("  Found existing cloudbuild.yaml file.")
        else:
            # Generate file from template.
            context = {
                'service': self.service_name, 
                'region': self.region,
                'image_name': self.image_name,
                'job_name': self.migrate_job_name,
                }
            path = self.sd.project_root / 'cloudbuild.yaml'
            write_file_from_template(path, 'cloudbuild.yaml', context)

            self.log(f"\n    Generated cloudbuild.yaml: {path}")
            return path


    def _modify_settings(self):
        """Add settings specific to Cloud Run."""
        #   Check if a cloudrun section is present. If not, add settings. If already present,
        #   do nothing.
        self.log("\n  Checking if settings block for Cloud Run present in settings.py...")

        with open(self.sd.settings_path) as f:
            settings_string = f.read()

        if 'if os.environ.get("ON_CLOUDRUN"):' in settings_string:
            self.log("\n    Found Cloud Run settings block in settings.py.")
            return

        # Add Cloud Run settings block.
        self.log("    No Cloud Run settings found in settings.py; adding settings...")

        safe_settings_string = mark_safe(settings_string)
        context = {
            'deployed_url': self.deployed_url.replace("https://", ""),
            'current_settings': safe_settings_string,
        }
        path = Path(self.sd.settings_path)
        write_file_from_template(path, 'settings.py', context)

        self.log(f"    Modified settings.py file: {path}")

    def _add_python_packages(self):
        packages = ["gunicorn", "psycopg2-binary", "whitenoise", "dj-database-url" ]

        self.log("Adding packages to project dependencies)")
        for name in packages:
            self.log(f"\n  Looking for {name}...")
            if self.sd.using_req_txt:
                self.sd.add_req_txt_pkg(name)
            elif self.sd.using_pipenv:
                self.sd.add_pipenv_pkg(name)


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
        self.log("  Deploying to Cloud Run...")
        self.run("gcloud builds submit", stream=True)

        # Open project.
        self.log("  Opening deployed app in a new browser tab...")
        self.log(self.deployed_url)


    def _show_success_message(self):
        """After a successful run, show a message about what to do next."""

        # DEV:
        # - Mention that this script should not need to be run again, unless
        #   creating a new deployment.
        #   - Describe ongoing approach of commit, push, migrate. Lots to consider
        #     when doing this on production app with users, make sure you learn.

        if self.sd.automate_all:
            self.log(cloudrun_msgs.success_msg_automate_all(self.deployed_url))
        else:
            self.log(cloudrun_msgs.success_msg(log_output=self.sd.log_output))


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
            # We are happily exiting the script; there's no need to raise a CommandError.
            self.stdout.write(cloudrun_msgs.cancel_cloudrun)
            sys.exit()


    def validate_platform(self):
        """Make sure the local environment and project supports deployment to
        Cloud Run.
        
        The returncode for a successful command is 0, so anything truthy means
          a command errored out.
        """
        self._validate_cli()


    def prep_automate_all(self):
        """Do intial work for automating entire process."""
        pass
    
    def _get_googlecloud_project(self):
        """Use the gcloud CLI to get the current active project"""
        self.log("Finding active Google Cloud project")

        _, project_id = self.run(f"gcloud config get-value project")
        if not project_id: 
            raise CommandError(cloudrun_msgs.no_project_id)

        _, project_num = self.run(f"gcloud projects describe {project_id} --format 'value(projectNumber)'")
        self.log(f"  Found Google Cloud project: {project_id}, num: {project_num}")

        self.project_id = project_id
        self.project_num = project_num


    def _get_googlerun_region(self):
        """Use passed configuration, or gcloud configuration"""
        self.log("Finding configured Cloud Run region")

        if self.sd.region: 
            # Backcompat: default CLI is platform specific, so don't use if it's probably the CLI default.
            if "platform.sh" not in self.sd.region:
                self.region = self.sd.region
                self.log(f"Using region: {self.region}")
                return

        self.log("Checking gcloud configuration")
        _, region = self.run(f"gcloud config get-value run/region")

        if not region: 
            self.log("No configuration found. Using 'us-central1'.")
            self.region = "us-central1"
            return
        self.region = region
        self.log(f"Using gcloud configured region: {self.region}")
        


    def _get_random_string(self, length=20):
        """Get a random string, for secret keys and database passwords"""
        return get_random_string(length,
                    allowed_chars='abcdefghijklmnopqrstuvwxyz0123456789')

    # --- Helper methods for methods called from simple_deploy.py ---

    def _validate_cli(self):
        """Make sure the Google Cloud CLI is installed."""
        return_code, _ = self.run("gcloud version")
        if return_code != 0:
            raise CommandError(cloudrun_msgs.cli_not_installed)


    def _create_db(self):
        """Create a postgres instance, database, user, and secret.

        This is a complex setup if any existing element is presumed to exist. 
        However, instance creation is long, so the only element that can be re-used is
        the instance. This will greatly help with testing.
        """

        self.instance_name = f"{self.service_name}-instance"
        self.database_name = "django-db" #TODO(glasnt) change?
        self.database_user = "django-user"
        self.instance_fqn = f"{self.project_id}:{self.region}:{self.instance_name}"
        self.database_pass = self._get_random_string()
        self.instance_pass = self._get_random_string()
        self.database_secret = f"{self.service_name}-database_url"

        self.log("Looking for a Postgres instance...")

        instance_exists = self._check_if_dbinstance_exists()

        if instance_exists:
            self.log("  Found existing instance.")
        else:
            self.log(f"  Create a new Postgres database (this may take a while)...")

            # TODO(glasnt) instance size?
            cmd = f"""gcloud sql instances create {self.instance_name} \
                        --database-version POSTGRES_14 --cpu 2 --memory 4GB  \
                        --region {self.region} \
                        --project {self.project_id} \
                        --root-password {self.instance_pass} \
                """

            # If not using automate_all, make sure it's okay to create a resource
            #   on user's account.
            if not self.sd.automate_all:
                self._confirm_create_instance(db_cmd=cmd)

            # Create database.
            # Use execute_command(), to stream output of long-running process.
            self.run(cmd, stream=True)
            self.log("  Created Postgres instance")

        db_exists = self._check_if_db_exists()
        if db_exists:
            self.log("  Database exists, using that one.")
        else:
            self.log("  Creating new database...")
            self.run(f"""gcloud sql databases create {self.database_name} \
                        --instance {self.instance_name}""")
            self.log("  Created Postgres database")
        

        user_exists = self._check_if_dbuser_exists()
        secret_exists = self._check_if_dbsecret_exists()

        if user_exists and secret_exists:
            self.log("  Database user and secret exists. This is okay.")
        elif user_exists and not secret_exists:
            self.log("  Database user exists, but password not stored. I'm sad.")
            raise CommandError(cloudrun_msgs.no_database_password)
        elif not user_exists and not secret_exists:
            self.log("  Database user and secret don't exist. Creating.")


            self.log("  Creating new user...")
            self.run(f"""gcloud sql users create {self.database_user} \
                        --instance {self.instance_name} \
                        --password {self.database_pass}
                        """)
            self.log("  Created database user.")

            self.log("  Creating database secret...")
            self.database_url = f"postgres://{self.database_user}:{self.database_pass}@//cloudsql/{self.instance_fqn}/{self.database_name}"
            with tempfile.NamedTemporaryFile() as fp: 
                fp.write(str.encode(self.database_url))
                fp.seek(0)
                self.run(f"gcloud secrets create {self.database_secret} --data-file {fp.name}")
            self.log("  Created secret")

            self.log("  Update permissions for secret")
            self.run(f"""gcloud secrets add-iam-policy-binding {self.database_secret} \
                            --member serviceAccount:{self.cloudrun_sa} \
                            --role roles/secretmanager.secretAccessor""")
            self.log("  Permissions updated.")


            self.log("  Assigning secret to service")
            self.run(f"gcloud run services update {self.service_name} --region {self.region} --update-env-vars DATABASE_URL={self.database_secret}:latest")
            self.log("  Assigned secret.")
        
        self._create_container()
        self._create_migrate_job()

    def _check_if_dbinstance_exists(self):
        """Check if a postgres instance already exists that should be used with this app.
        Returns:
        - True if db found.
        - False if not found.
        """

        # First, see if any Postgres instances exist.
        _, return_str = self.run("gcloud sql instances list")

        if "Listed 0 items" in return_str:
            self.log("  No Postgres instance found.")
            return False
        elif self.instance_name in return_str:
            self.log("  Postgres instance was found.")
            return True
        else:
            self.log("  A Postgres instance was found, but not what we expected.")
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

        self.log(cloudrun_msgs.confirm_create_instance(re.sub(' +', ' ',db_cmd)))
        confirmed = self.sd.get_confirmation(skip_logging=True)

        if not confirmed:
            # Quit and invite the user to create a database manually.
            raise CommandError(cloudrun_msgs.cancel_no_instance)

    def _check_if_db_exists(self):
        """Check if a postgres datbase already exists that should be used with this app.
        Returns:
        - True if db found.
        - False if not found.
        """

        # First, see if any Postgres instances exist.
        _, return_str = self.run(f"gcloud sql databases list --instance {self.instance_name}")

        if self.database_name in return_str:
            self.log(f"  Database {self.database_name} found")
            return True
        else:
            self.log("  Database not found")
            return False


    def _check_if_dbuser_exists(self):
        """Check if a postgres user already exists that should be used with this app.
        """
        _, return_str = self.run(f"""gcloud sql users list \
                --instance {self.instance_name} \
                --filter \"name:{self.database_user}\"""")

        if self.database_user in return_str:
            self.log(f"  User {self.database_user} found")
            return True
        else:
            self.log("  User not found")
            return False


    def _check_if_dbsecret_exists(self):
        """Check if a secret already exists that should be used with this app.
        """
        _, return_str = self.run(f"gcloud secrets list") # --filter \"name:{self.database.secret}\"")
      

        if self.database_secret in return_str:
            self.log(f"  Secret {self.database_secret} found")
            return True
        else:
            self.log("  Secret not found")
            return False
