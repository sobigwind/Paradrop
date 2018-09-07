class Service(object):
    """
    A service is a long-running process that provides chute functionality.
    """

    def __init__(self,
                 chute=None,
                 name=None,
                 type="normal",
                 source=None,
                 image=None,
                 command=None,
                 dockerfile=None,
                 build=None,
                 environment=None,
                 interfaces=None,
                 requests=None):
        self.chute = chute
        self.name = name

        self.type = type
        self.source = source
        self.image = image
        self.command = command
        self.dockerfile = dockerfile

        if build is None:
            self.build = {}
        else:
            self.build = build

        if environment is None:
            self.environment = {}
        else:
            self.environment = environment

        if interfaces is None:
            self.interfaces = {}
        else:
            self.interfaces = interfaces

        if requests is None:
            self.requests = {}
        else:
            self.requests = requests

    def create_specification(self):
        """
        Create a new service specification.

        This is a completely clean copy of all information necessary to rebuild
        the Service object. It should contain only primitive types, which can
        easily be serialized as JSON or YAML.
        """
        spec = {
            "type": self.type,
            "source": self.source,
            "image": self.image,
            "command": self.command,
            "build": self.build.copy(),
            "environment": self.environment.copy(),
            "interfaces": self.interfaces.copy(),
            "requests": self.requests.copy()
        }
        return spec

    def get_container_name(self):
        """
        Get the name for the service's container.

        This will be a combination of the chute name and the service name.
        """
        if self.name is None:
            # name can be None for old-style single-service chutes where the
            # container name is expected to be the name of the chute.
            return self.chute.name
        else:
            return "{}-{}".format(self.chute.name, self.name)

    def get_image_name(self):
        """
        Get the name of the image to be used.
        """
        # Light chute services have a shorthand image name like "python2" that
        # should not be interpreted as an actual Docker image name.
        if self.image is None or self.type == "light":
            return "{}:{}".format(self.get_container_name(), self.chute.version)
        else:
            return self.image
