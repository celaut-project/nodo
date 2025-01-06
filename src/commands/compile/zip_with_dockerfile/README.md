# Service Configuration Guide

## Directory Structure
A service can be structured in two main ways:

### Option 1: Configuration in .service directory (Recommended)
```
my-service/
├── .service/
│   ├── Dockerfile
│   ├── service.json
│   ├── pre-compile.json
│   └── .dockerignore
├── src/
│   └── app.py
├── tests/
│   └── test_app.py
├── requirements.txt
└── README.md
```

### Option 2: Root Configuration (Simple)
```
my-service/
├── Dockerfile
├── service.json
├── .dockerignore
├── src/
│   └── app.py
└── requirements.txt
```

The script will automatically:
1. Create the `.service` directory
2. Copy Dockerfile, service.json, and .dockerignore to `.service`
3. Create a default pre-compile.json with `{"ignore": []}`

This means both structures will end up with everything properly organized in `.service` during the build process.

## Configuration Files

### 1. pre-compile.json
This is the main configuration file that controls how the service is built and packaged.

```json
{
    "ignore": [],                           // Files/patterns to exclude
    "include": [],                          // Optional: Specific files/folders to include
    "zip": false,                           // Optional: Whether to zip dependencies
    "service_dependencies_directory": "",    // Directory for service dependencies
    "metadata_dependencies_directory": "",   // Directory for metadata dependencies
    "blocks_directory": "",                 // Directory for blocks
    "dependencies_dir": {}                  // Service dependencies configuration
}
```

#### Key Configuration Options:
- `ignore`: Array of files or patterns to exclude from the build (similar to .dockerignore)
- `include`: Optional array of specific files or directories to include
- `zip`: Boolean flag to determine if dependencies should be zipped
- `service_dependencies_directory`: Path where service dependencies will be stored
- `metadata_dependencies_directory`: Path where metadata files will be stored
- `blocks_directory`: Path where blocks will be stored
- `dependencies_dir`: Object or array containing service dependencies

### 2. Dockerfile
The Dockerfile can be placed either in the root directory or in the `.service` directory. If placed in the root, it will be automatically copied to `.service` during preparation.

Important notes about COPY instructions:
- When using relative paths in COPY instructions, they will be automatically adjusted
- Example: `COPY ./src` will be converted to `COPY service/src`

### 3. service.json
Service metadata file that can be placed either in the root directory or in `.service`. Contains service-specific configuration and metadata.

### 4. .dockerignore
Can be placed either in the root directory or in `.service`. Patterns specified here will be automatically added to the `ignore` list in `pre-compile.json`.

## Service Preparation Process

The service preparation happens in two main stages:

### 1. Directory Preparation (`prepare_directory`)
- Handles both local directories and Git repositories
- Creates a cache directory with a unique identifier
- Ensures correct structure by creating `.service` directory if missing
- Copies and adjusts configuration files as needed
- Processes .dockerignore patterns
- Adjusts Dockerfile COPY commands

### 2. Service ZIP Generation (`generate_service_zip`)
1. Creates a temporary `service` directory inside `.service`
2. Copies all files based on include/ignore rules
3. Processes dependencies:
   - Copies service dependencies
   - Copies metadata dependencies
   - Copies required blocks
4. Optionally zips dependencies if configured
5. Creates final `.service.zip` package
6. Cleans up temporary files

## Best Practices

1. **File Organization**
   - Keep configuration files in `.service` directory
   - Use clear and consistent paths in configuration
   - Maintain a clean root directory structure

2. **Dependencies**
   - Clearly specify all required dependencies
   - Use appropriate directory structures for different dependency types
   - Consider whether dependencies should be zipped

3. **Ignore Patterns**
   - Use .dockerignore for Docker-specific exclusions
   - Use pre-compile.json's ignore list for service-specific exclusions
   - Avoid duplicating ignore patterns

4. **Build Configuration**
   - Keep pre-compile.json up to date
   - Use include list when specific files are needed
   - Configure appropriate dependency directories

## Common Issues and Solutions

1. **Missing Dependencies**
   - Ensure all dependencies are properly listed in dependencies_dir
   - Verify paths in dependency directories exist

2. **Build Failures**
   - Check Dockerfile COPY commands for correct paths
   - Verify ignore patterns aren't excluding required files
   - Ensure all required configuration files are present

3. **Package Size Issues**
   - Review ignore patterns to exclude unnecessary files
   - Consider using include list to limit included files
   - Use appropriate .dockerignore patterns
   