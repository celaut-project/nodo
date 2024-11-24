# Guide to Compile a Service

<aside>
🚧 
While this is an explanation of how to perform the compilation, the `nodo compile <directory>` command can also be used.

</aside>

To create a service in Docker, you can follow the steps below:

1. Create a folder called ".service" and inside it, create a "Dockerfile" that contains the instructions to build the service. You can also include a "service.json" file specifying details such as the API, entrypoint, and service architecture.
2. Compress the ".service" folder into a ".zip" file and send it to the node for compilation. The node will return a buffer of the built service.
3. One way to mount the Dockerfile is to use a "git clone" instruction to obtain the project code and mount the directory with the code in the container.
4. If the service depends on other services or heavy files, you can use a "COPY" instruction to copy the necessary files into the container. This can be done by creating a "src" subdirectory within the ".service" folder and adding the required files to it.
5. If dependencies are too heavy, they can be compressed into a ".zip" file and decompressed when starting the service instance. This can help reduce the size of the service and improve its performance.

<aside>
💡  The service.json should be included within CompileInput, and only leave

</aside>