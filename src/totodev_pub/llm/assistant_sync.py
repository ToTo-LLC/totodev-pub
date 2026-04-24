# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

from openai import OpenAI
from openai import AzureOpenAI  
from pydantic import BaseModel, computed_field, Field
from typing import Optional, Tuple, Self, Dict, List
import datetime
import hashlib
import json
from enum import Enum

from totodev_pub.file_mapped_pydantic_mixin import FileMappedPydanticMixin

class ConnectionError(Exception):
    """Raised when connection to OpenAI/Azure OpenAI fails."""
    pass

class AssistantError(Exception):
    """Base class for assistant-related errors."""
    pass

class AssistantNotFoundError(AssistantError):
    """Raised when an assistant cannot be found."""
    pass

class AssistantOrgin(BaseModel):
    """ A class for defining an online origin where assistant definition is stored (such as OpenAI or Azure OpenAI).  """
    assistant_id: str # the id of the assistant at this origin
    last_fetchwrite: datetime.datetime # last time was fetched from origin or written to origin
    last_payload_hash: Optional[str] = None # md5 hash of the last payload read or written from this origin

class OpenAIAssistantDefinition(BaseModel, FileMappedPydanticMixin):
    """
    A class for representing a local snapshot of an OpenAI assistant.

    Includes functions for creating, updating, retrieving, and deleteing assistant definitions.

    See documentation nested at: https://platform.openai.com/docs/assistants/overview
    """
    
    @computed_field
    def auto_notes(self) -> str:
        """ Return a string of auto-generated notes about the assistant. """
        if not self.origins:
            return "⚠️ This is a local-only assistant definition not synced with any online service."

        notes = []
        current_hash = self.payload_hash
        
        for assistant_id, origin in self.origins.items():
            # Determine service type from assistant ID prefix (Azure uses 'az_asst_' prefix)
            service_type = "Azure OpenAI" if assistant_id.startswith("az_asst_") else "OpenAI"
            
            # Add sync status
            sync_time = origin.last_fetchwrite.strftime("%Y-%m-%d %H:%M:%S")
            notes.append(f"📍 Last synced with {service_type} (ID: {assistant_id}) at {sync_time}")
            
            # Add drift warning if hashes don't match
            if origin.last_payload_hash and origin.last_payload_hash != current_hash:
                notes.append(f"⚠️ Local definition differs from {service_type} version")
        
        notes.append("\n⚡ Note: This is a local definition - changes here won't affect the online assistant until explicitly synced.")
        
        return "\n".join(notes)
     
    origins: Dict[str, AssistantOrgin] = Field(default_factory=dict)  # Dictionary keyed by assistant_id
    definition_payload: dict = Field(default_factory=dict)

    @computed_field
    def payload_hash(self) -> str:
        """ Return the md5 hash of json-ified, key-sorted definition_payload. """
        return hashlib.md5(json.dumps(self.definition_payload, sort_keys=True).encode()).hexdigest()

    def _conn(self, api_key: str, azure_api_verion: Optional[str]=None, azure_endpoint_url: Optional[str]=None) -> Tuple[OpenAI, str]:
        """ Return a tuple of the OpenAI object and the connection type.
        
        Args:
            api_key: The OpenAI API key
            azure_api_verion: Optional Azure API version if using Azure OpenAI
            azure_endpoint_url: Optional Azure endpoint URL if using Azure OpenAI
            
        Returns:
            Tuple[OpenAI, str]: A tuple containing the client object and connection type
            
        Raises:
            ConnectionError: If connection fails or parameters are invalid
        """
        try:
            if azure_api_verion and azure_endpoint_url:
                return AzureOpenAI(
                    api_version=azure_api_verion,
                    azure_endpoint=azure_endpoint_url,
                    api_key=api_key
                ), "AzureOpenAI"
            else:
                return OpenAI(api_key=api_key), "OpenAI"
        except Exception as e:
            service = "Azure OpenAI" if azure_api_verion and azure_endpoint_url else "OpenAI"
            raise ConnectionError(f"Failed to establish connection to {service}: {str(e)}")

    def retrieve(self, assistant_id: str, api_key: str, azure_api_verion: Optional[str]=None, azure_endpoint_url: Optional[str]=None) -> Self:
        """Retrieve an assistant definition from the origin.

        Args:
            assistant_id: The ID of the assistant to retrieve
            api_key: The OpenAI API key
            azure_api_verion: Optional Azure API version if using Azure OpenAI
            azure_endpoint_url: Optional Azure endpoint URL if using Azure OpenAI

        Returns:
            Self: Returns self for method chaining

        Raises:
            ConnectionError: If connection to OpenAI/Azure OpenAI fails
            ValueError: If the assistant_id is not found or invalid
            Exception: For other unexpected errors during retrieval
        """
        # Determine which service we're connecting to for error messaging
        service_type = "Azure OpenAI" if azure_api_verion and azure_endpoint_url else "OpenAI"
        
        try:
            # Create an OpenAI or AzureOpenAI client depending on params        
            oai, conn_type = self._conn(api_key, azure_api_verion, azure_endpoint_url)
        except Exception as e:
            raise ConnectionError(f"Failed to establish connection to {service_type}. Error: {str(e)}")

        try:
            # Get the assistant definition
            assistant = oai.beta.assistants.retrieve(assistant_id=assistant_id)
        except Exception as e:
            if "not found" in str(e).lower():
                raise AssistantNotFoundError(f"Assistant with ID '{assistant_id}' not found on {service_type}")
            elif "unauthorized" in str(e).lower():
                raise ConnectionError(f"Authentication failed for {service_type}. Please check your API key")
            else:
                raise Exception(f"Error retrieving assistant from {service_type}: {str(e)}")

        # Create/update the origin entry
        origin = AssistantOrgin(
            assistant_id=assistant_id,
            last_fetchwrite=datetime.datetime.now(),
            last_payload_hash=self.payload_hash
        )

        # Update the origins dictionary with the new origin
        self.origins[assistant_id] = origin
        self.definition_payload = assistant
        return self

    def create(self, api_key: str, azure_api_verion: Optional[str]=None, azure_endpoint_url: Optional[str]=None) -> Self:
        """Create a new assistant using the current definition_payload.

        Args:
            api_key: The OpenAI API key
            azure_api_verion: Optional Azure API version if using Azure OpenAI
            azure_endpoint_url: Optional Azure endpoint URL if using Azure OpenAI

        Returns:
            Self: Returns self for method chaining

        Raises:
            ConnectionError: If connection to OpenAI/Azure OpenAI fails
            ValueError: If the payload is invalid
            Exception: For other unexpected errors during creation
        """
        # Determine which service we're connecting to for error messaging
        service_type = "Azure OpenAI" if azure_api_verion and azure_endpoint_url else "OpenAI"
        
        try:
            # Create an OpenAI or AzureOpenAI client depending on params        
            oai, conn_type = self._conn(api_key, azure_api_verion, azure_endpoint_url)
        except Exception as e:
            raise ConnectionError(f"Failed to establish connection to {service_type}. Error: {str(e)}")

        try:
            # Create the assistant
            assistant = oai.beta.assistants.create(**self.definition_payload)
            assistant_id = assistant.id
        except Exception as e:
            if "invalid" in str(e).lower():
                raise ValueError(f"Invalid assistant definition for {service_type}: {str(e)}")
            elif "unauthorized" in str(e).lower():
                raise ConnectionError(f"Authentication failed for {service_type}. Please check your API key")
            else:
                raise Exception(f"Error creating assistant on {service_type}: {str(e)}")

        # Create the origin entry
        origin = AssistantOrgin(
            assistant_id=assistant_id,
            last_fetchwrite=datetime.datetime.now(),
            last_payload_hash=self.payload_hash
        )

        # Update the origins dictionary with the new origin
        self.origins[assistant_id] = origin
        self.definition_payload = assistant
        return self

    def update(self, assistant_id: str, api_key: str, azure_api_verion: Optional[str]=None, azure_endpoint_url: Optional[str]=None) -> Self:
        """Update an existing assistant with the current definition_payload.

        Args:
            assistant_id: The ID of the assistant to update
            api_key: The OpenAI API key
            azure_api_verion: Optional Azure API version if using Azure OpenAI
            azure_endpoint_url: Optional Azure endpoint URL if using Azure OpenAI

        Returns:
            Self: Returns self for method chaining

        Raises:
            ConnectionError: If connection to OpenAI/Azure OpenAI fails
            AssistantNotFoundError: If the assistant_id is not found
            ValueError: If the payload is invalid
            Exception: For other unexpected errors during update
        """
        # Determine which service we're connecting to for error messaging
        service_type = "Azure OpenAI" if azure_api_verion and azure_endpoint_url else "OpenAI"
        
        try:
            # Create an OpenAI or AzureOpenAI client depending on params        
            oai, conn_type = self._conn(api_key, azure_api_verion, azure_endpoint_url)
        except Exception as e:
            raise ConnectionError(f"Failed to establish connection to {service_type}. Error: {str(e)}")

        try:
            # Update the assistant
            assistant = oai.beta.assistants.update(assistant_id=assistant_id, **self.definition_payload)
        except Exception as e:
            if "not found" in str(e).lower():
                raise AssistantNotFoundError(f"Assistant with ID '{assistant_id}' not found on {service_type}")
            elif "invalid" in str(e).lower():
                raise ValueError(f"Invalid assistant definition for {service_type}: {str(e)}")
            elif "unauthorized" in str(e).lower():
                raise ConnectionError(f"Authentication failed for {service_type}. Please check your API key")
            else:
                raise Exception(f"Error updating assistant on {service_type}: {str(e)}")

        # Create/update the origin entry
        origin = AssistantOrgin(
            assistant_id=assistant_id,
            last_fetchwrite=datetime.datetime.now(),
            last_payload_hash=self.payload_hash
        )

        # Update the origins dictionary with the new origin
        self.origins[assistant_id] = origin
        self.definition_payload = assistant
        return self

    def delete(self, assistant_id: str, api_key: str, azure_api_verion: Optional[str]=None, azure_endpoint_url: Optional[str]=None) -> bool:
        """Delete an assistant.

        Args:
            assistant_id: The ID of the assistant to delete
            api_key: The OpenAI API key
            azure_api_verion: Optional Azure API version if using Azure OpenAI
            azure_endpoint_url: Optional Azure endpoint URL if using Azure OpenAI

        Returns:
            bool: True if deletion was successful

        Raises:
            ConnectionError: If connection to OpenAI/Azure OpenAI fails
            AssistantNotFoundError: If the assistant_id is not found
            Exception: For other unexpected errors during deletion
        """
        service_type = "Azure OpenAI" if azure_api_verion and azure_endpoint_url else "OpenAI"
        
        try:
            oai, conn_type = self._conn(api_key, azure_api_verion, azure_endpoint_url)
        except Exception as e:
            raise ConnectionError(f"Failed to establish connection to {service_type}. Error: {str(e)}")

        try:
            deletion = oai.beta.assistants.delete(assistant_id=assistant_id)
            if deletion.deleted:
                if assistant_id in self.origins:
                    del self.origins[assistant_id]
                return True
            return False
        except Exception as e:
            if "not found" in str(e).lower():
                raise AssistantNotFoundError(f"Assistant with ID '{assistant_id}' not found on {service_type}")
            elif "unauthorized" in str(e).lower():
                raise ConnectionError(f"Authentication failed for {service_type}. Please check your API key")
            else:
                raise Exception(f"Error deleting assistant from {service_type}: {str(e)}")

    @staticmethod
    def list_assistants(api_key: str, azure_api_verion: Optional[str]=None, azure_endpoint_url: Optional[str]=None, 
                       limit: int = 20, order: str = "desc", after: Optional[str] = None, before: Optional[str] = None) -> List[dict]:
        """List all assistants.

        Args:
            api_key: The OpenAI API key
            azure_api_verion: Optional Azure API version if using Azure OpenAI
            azure_endpoint_url: Optional Azure endpoint URL if using Azure OpenAI
            limit: Maximum number of assistants to return (1-100)
            order: Sort order by created_at timestamp ("asc" or "desc")
            after: A cursor for pagination (assistant ID to fetch results after)
            before: A cursor for pagination (assistant ID to fetch results before)

        Returns:
            List[dict]: List of assistant definitions

        Raises:
            ConnectionError: If connection to OpenAI/Azure OpenAI fails
            ValueError: If pagination parameters are invalid
            Exception: For other unexpected errors
        """
        service_type = "Azure OpenAI" if azure_api_verion and azure_endpoint_url else "OpenAI"
        
        try:
            if azure_api_verion and azure_endpoint_url:
                client = AzureOpenAI(api_version=azure_api_verion, azure_endpoint=azure_endpoint_url, api_key=api_key)
            else:
                client = OpenAI(api_key=api_key)
        except Exception as e:
            raise ConnectionError(f"Failed to establish connection to {service_type}. Error: {str(e)}")

        try:
            return client.beta.assistants.list(
                limit=limit,
                order=order,
                after=after,
                before=before
            )
        except Exception as e:
            if "unauthorized" in str(e).lower():
                raise ConnectionError(f"Authentication failed for {service_type}. Please check your API key")
            elif "invalid" in str(e).lower():
                raise ValueError(f"Invalid pagination parameters: {str(e)}")
            else:
                raise Exception(f"Error listing assistants from {service_type}: {str(e)}")





