import os
import time
import re
from datetime import datetime
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from bson import ObjectId
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from dotenv import load_dotenv
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError, GeocoderUnavailable
from geopy.adapters import RequestsAdapter
import ssl
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Load environment variables
load_dotenv()

VIEWPOINT_USER = os.getenv('VIEWPOINT_USER')
VIEWPOINT_PASS = os.getenv('VIEWPOINT_PASS')
MONGODB_URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/')
MONGODB_DB_NAME = os.getenv('MONGODB_DB_NAME', 'viewpoint_properties')
MONGODB_COLLECTION_NAME = os.getenv('MONGODB_COLLECTION_NAME', 'properties')

def setup_driver():
    """Setup and return a Chrome WebDriver instance"""
    chrome_options = Options()
    # Run headless (no browser window) for faster performance
    chrome_options.add_argument('--headless=new')  # Use new headless mode (faster than old headless)
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-blink-features=AutomationControlled')
    # Performance optimizations for headless mode
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--disable-extensions')
    chrome_options.add_argument('--disable-background-timer-throttling')
    chrome_options.add_argument('--disable-backgrounding-occluded-windows')
    chrome_options.add_argument('--disable-renderer-backgrounding')
    # Note: We keep JavaScript enabled as viewpoint.ca requires it for dynamic content
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    
    driver = webdriver.Chrome(options=chrome_options)
    return driver

def login_to_viewpoint(driver):
    """Login to viewpoint.ca using credentials from .env file"""
    login_url = "https://viewpoint.ca/user/login"
    driver.get(login_url)
    
    # Wait for the login form to load
    wait = WebDriverWait(driver, 15)
    
    # Find and fill in the email field - wait for it to be clickable
    email_input = wait.until(
        EC.element_to_be_clickable((By.NAME, "login-email"))
    )
    email_input.clear()
    email_input.send_keys(VIEWPOINT_USER)
    
    # Find and fill in the password field - wait for it to be clickable
    password_input = wait.until(
        EC.element_to_be_clickable((By.NAME, "login-password"))
    )
    password_input.clear()
    password_input.send_keys(VIEWPOINT_PASS)
    
    # Press Enter to submit the login form
    print("Pressing Enter to submit login...")
    password_input.send_keys(Keys.RETURN)
    
    # Wait for login to complete - check for URL change or page change
    print("Waiting for login to complete...")
    time.sleep(3)
    
    # Check if we're still on the login page or if we've moved on
    current_url = driver.current_url
    print(f"Current URL after login: {current_url}")
    
    if "login" not in current_url.lower():
        print("Login successful - page changed!")
    else:
        print("Warning: May still be on login page, but continuing...")
    
    print("Login attempt completed!")

def navigate_to_new_today_list(driver):
    """Navigate to the new-today-list page"""
    new_today_url = "https://www.viewpoint.ca/user#!/new-today-list/"
    driver.get(new_today_url)
    
    # Wait for the page to load
    time.sleep(5)
    
    print(f"Navigated to: {new_today_url}")

def extract_all_links(driver):
    """Extract cutsheet links from the current page and print them to console"""
    # Wait for page content to load
    wait = WebDriverWait(driver, 10)
    
    # Find all link elements
    all_links = driver.find_elements(By.TAG_NAME, "a")
    
    # Filter for only cutsheet links
    cutsheet_links = []
    for link in all_links:
        href = link.get_attribute("href")
        if href and "cutsheet" in href.lower():
            text = link.text.strip()
            cutsheet_links.append({"href": href, "text": text})
    
    print("\n=== Cutsheet Links Found ===")
    for i, link_data in enumerate(cutsheet_links, 1):
        print(f"{i}. {link_data['text']} -> {link_data['href']}")
    
    print(f"\nTotal cutsheet links found: {len(cutsheet_links)}")
    return cutsheet_links

def extract_photos(driver):
    """Click on the photo element, open the viewer, and collect all photo URLs"""
    photo_urls = []
    try:
        print("\n--- Extracting Photos ---")
        
        # Find and click the photo element to open the viewer
        wait = WebDriverWait(driver, 10)
        try:
            photo_element = wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "div.lzimg.lzimg-active.lzimg-loaded"))
            )
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", photo_element)
            time.sleep(0.5)
            
            # Click to open photo viewer
            try:
                photo_element.click()
            except:
                driver.execute_script("arguments[0].click();", photo_element)
            
            print("Clicked on photo to open viewer")
            time.sleep(2)  # Wait for photo viewer to open
            
        except Exception as e:
            print(f"Could not click photo element: {e}")
            return photo_urls
        
        # Wait for the photo viewer to be visible and get the counter
        try:
            # Get total number of photos from the counter
            counter_all = wait.until(
                EC.presence_of_element_located((By.CLASS_NAME, "lg-counter-all"))
            )
            total_photos = int(counter_all.text.strip())
            print(f"Total photos found: {total_photos}")
            
            # Collect photos by navigating through them
            for i in range(total_photos):
                try:
                    # Wait a moment for the photo to load
                    time.sleep(0.3)
                    
                    # Try multiple ways to get the current photo URL
                    photo_url = None
                    
                    # Method 1: Try to find img element in the viewer
                    try:
                        photo_img = driver.find_element(By.CSS_SELECTOR, "div.lg-current img, img.lg-image, .lg-item.active img")
                        photo_url = photo_img.get_attribute("src")
                        if not photo_url:
                            photo_url = photo_img.get_attribute("data-src")
                    except:
                        pass
                    
                    # Method 2: Try to get from background image style
                    if not photo_url:
                        try:
                            photo_container = driver.find_element(By.CSS_SELECTOR, "div.lg-current, .lg-item.active")
                            style = photo_container.get_attribute("style")
                            if style and "url(" in style:
                                match = re.search(r'url\(["\']?([^"\']+)["\']?\)', style)
                                if match:
                                    photo_url = match.group(1)
                        except:
                            pass
                    
                    # Method 3: Try data-src attribute on the container
                    if not photo_url:
                        try:
                            photo_container = driver.find_element(By.CSS_SELECTOR, "div.lg-current, .lg-item.active")
                            photo_url = photo_container.get_attribute("data-src")
                        except:
                            pass
                    
                    if photo_url:
                        # Clean up the URL (remove HTML entities)
                        photo_url = photo_url.replace("&amp;", "&")
                        if photo_url not in photo_urls:
                            photo_urls.append(photo_url)
                            print(f"  Photo {i+1}/{total_photos}: {photo_url}")
                    else:
                        print(f"  Warning: Could not extract URL for photo {i+1}")
                    
                    # If not the last photo, navigate to the next one
                    if i < total_photos - 1:
                        # Try to find and click the next button or use arrow key
                        try:
                            next_button = driver.find_element(By.CSS_SELECTOR, "button.lg-next, .lg-next, button[aria-label*='Next']")
                            next_button.click()
                        except:
                            # If no next button, try arrow key
                            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ARROW_RIGHT)
                        
                        time.sleep(0.5)  # Wait for next photo to load
                    
                except Exception as e:
                    print(f"Error extracting photo {i+1}: {e}")
                    # Try to continue to next photo anyway
                    if i < total_photos - 1:
                        try:
                            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ARROW_RIGHT)
                            time.sleep(0.5)
                        except:
                            pass
            
            # Close the photo viewer (press Escape or click close button)
            try:
                close_button = driver.find_element(By.CSS_SELECTOR, "button.lg-close, .lg-close")
                close_button.click()
            except:
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            
            time.sleep(1)
            
        except Exception as e:
            print(f"Error in photo extraction: {e}")
            # Try to close the viewer if it's open
            try:
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            except:
                pass
        
        print(f"Total photos collected: {len(photo_urls)}")
        
    except Exception as e:
        print(f"Error extracting photos: {e}")
        import traceback
        traceback.print_exc()
    
    return photo_urls

# Initialize geocoder once (geopy handles rate limiting internally)
_geocoder = None

def get_geocoder():
    """Get or create the Nominatim geocoder instance"""
    global _geocoder
    if _geocoder is None:
        # Create adapter with SSL verification disabled to handle certificate issues
        # This is safe for local scraping use
        try:
            import ssl
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            adapter = RequestsAdapter(
                proxies=None,
                ssl_context=ssl_context
            )
            _geocoder = Nominatim(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                timeout=30,  # Increased timeout
                adapter_factory=lambda: adapter
            )
        except Exception as e:
            print(f"Warning: Could not set up custom adapter, using default: {e}")
            # Fallback: try without custom adapter
            _geocoder = Nominatim(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                timeout=30
            )
    return _geocoder

def geocode_address(address):
    """Geocode an address to get GPS coordinates using Nominatim (OpenStreetMap)"""
    if not address or not address.strip():
        print("  No address provided for geocoding")
        return None
    
    # Use direct API method first since we know it works from testing
    print(f"  Geocoding address: {address}")
    result = geocode_address_direct(address)
    
    if result:
        return result
    
    # If direct method fails, try geopy as fallback
    try:
        query_address = f"{address}, Nova Scotia, Canada"
        geolocator = get_geocoder()
        location = geolocator.geocode(
            query_address,
            country_codes="ca",
            exactly_one=True,
            timeout=30
        )
        
        if location:
            lat = location.latitude
            lon = location.longitude
            
            if 43.4 <= lat <= 47.0 and -66.4 <= lon <= -59.7:
                print(f"  ✓ Coordinates found (geopy fallback): {lat}, {lon}")
                return {
                    "latitude": lat,
                    "longitude": lon,
                    "display_name": location.address
                }
    except Exception as e:
        print(f"  ✗ Geopy fallback also failed: {e}")
    
    return None

def geocode_address_direct(address):
    """Primary geocoding using direct API calls (this method works from testing)"""
    if not address or not address.strip():
        return None
    
    # Try multiple address formats for better matching
    address_formats = [
        f"{address}, Nova Scotia, Canada",  # Full address
        address,  # Just the address without additions
    ]
    
    # Extract town name if address contains a comma (e.g., "123 Street, Town")
    if "," in address:
        parts = address.split(",")
        if len(parts) >= 2:
            town = parts[-1].strip()
            address_formats.append(f"{town}, Nova Scotia, Canada")
    
    url = "https://nominatim.openstreetmap.org/search"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }
    
    for query_address in address_formats:
        try:
            params = {
                "q": query_address,
                "format": "json",
                "limit": 1,
                "countrycodes": "ca",
            }
            
            response = requests.get(url, params=params, headers=headers, timeout=30, verify=False)
            
            if response.status_code == 200:
                data = response.json()
                if data and len(data) > 0:
                    result = data[0]
                    lat = float(result.get("lat", 0))
                    lon = float(result.get("lon", 0))
                    
                    # Verify it's in Nova Scotia
                    if 43.4 <= lat <= 47.0 and -66.4 <= lon <= -59.7:
                        print(f"  ✓ Coordinates found: {lat}, {lon}")
                        return {
                            "latitude": lat,
                            "longitude": lon,
                            "display_name": result.get("display_name", "")
                        }
                    else:
                        print(f"  ⚠ Coordinates outside Nova Scotia bounds: {lat}, {lon}")
                        # Continue to next format
                        continue
                else:
                    # No results for this format, try next
                    continue
            else:
                print(f"  ⚠ API returned status code: {response.status_code} for format: {query_address[:50]}")
                continue
            
        except requests.exceptions.Timeout:
            print(f"  ⚠ Request timed out for format: {query_address[:50]}")
            continue
        except Exception as e:
            print(f"  ⚠ Error with format '{query_address[:50]}': {e}")
            continue
        finally:
            # Rate limiting: Nominatim requires max 1 request per second
            time.sleep(1.1)  # Slightly more than 1 second to be safe
    
    print(f"  ✗ No coordinates found after trying {len(address_formats)} address formats")
    return None

def extract_property_details(driver, cutsheet_url):
    """Visit a cutsheet page, click Details, and extract property information"""
    try:
        print(f"\n{'='*60}")
        print(f"Visiting: {cutsheet_url}")
        print(f"{'='*60}")
        
        # Navigate to the cutsheet page
        driver.get(cutsheet_url)
        time.sleep(3)  # Wait for page to load
        
        wait = WebDriverWait(driver, 15)
        
        # Find and click the Details section
        try:
            # Try to find the Details section title (which is clickable)
            details_section = wait.until(
                EC.presence_of_element_located((By.XPATH, "//div[@class='cutsheet-section-title' and contains(text(), 'Details')]"))
            )
            
            # Scroll into view and click
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", details_section)
            time.sleep(0.5)
            
            # Try clicking the Details section
            try:
                details_section.click()
            except:
                # If regular click fails, use JavaScript
                driver.execute_script("arguments[0].click();", details_section)
            
            print("Clicked on Details section")
            time.sleep(1)  # Wait for details to expand
            
        except Exception as e:
            print(f"Warning: Could not click Details section: {e}")
            print("Attempting to extract details anyway...")
        
        property_data = {}
        
        # Extract Price, Status, and Address from the page
        print("\n--- Extracting Price, Status, and Address ---")
        
        # Extract Price
        try:
            price_element = driver.find_element(By.CLASS_NAME, "overlay-price")
            price = price_element.text.strip()
            property_data["Price"] = price
            print(f"Price: {price}")
        except Exception as e:
            print(f"Price not found: {e}")
            property_data["Price"] = ""
        
        # Extract Status
        try:
            status_element = driver.find_element(By.CLASS_NAME, "overlay-status")
            status = status_element.text.strip()
            property_data["Status"] = status
            print(f"Status: {status}")
        except Exception as e:
            print(f"Status not found: {e}")
            property_data["Status"] = ""
        
        # Extract Address
        try:
            address_element = driver.find_element(By.CLASS_NAME, "cutsheet-address")
            address = address_element.text.strip()
            property_data["Address"] = address
            print(f"Address: {address}")
        except Exception as e:
            print(f"Address not found: {e}")
            property_data["Address"] = ""
        
        # Geocode address to get GPS coordinates
        if property_data.get("Address"):
            print("\n--- Geocoding Address ---")
            coordinates = geocode_address(property_data["Address"])
            if coordinates:
                property_data["latitude"] = coordinates["latitude"]
                property_data["longitude"] = coordinates["longitude"]
                property_data["geocoded_address"] = coordinates.get("display_name", "")
                print(f"GPS Coordinates: {coordinates['latitude']}, {coordinates['longitude']}")
            else:
                print("Could not geocode address - coordinates not added")
                property_data["latitude"] = None
                property_data["longitude"] = None
        else:
            print("No address available for geocoding")
            property_data["latitude"] = None
            property_data["longitude"] = None
        
        # Extract Photos
        photo_urls = extract_photos(driver)
        property_data["Photos"] = photo_urls
        property_data["Photo_Count"] = len(photo_urls)
        
        # Extract property details from cutsheet-section-content
        try:
            # Try to find the content section - wait for it to be present
            print("Looking for cutsheet-section-content...")
            try:
                content_section = wait.until(
                    EC.presence_of_element_located((By.CLASS_NAME, "cutsheet-section-content"))
                )
                print("Found cutsheet-section-content")
            except:
                # Try alternative selectors
                print("Trying alternative selectors for content section...")
                content_section = driver.find_element(By.CSS_SELECTOR, "div.cutsheet-section-content")
                print("Found content section via CSS selector")
            
            # Wait a bit more for content to load
            time.sleep(2)
            
            # Find all detail items
            print("Looking for cutsheet-detail-item elements...")
            detail_items = content_section.find_elements(By.CLASS_NAME, "cutsheet-detail-item")
            print(f"Found {len(detail_items)} detail items")
            
            # If no items found, try searching the entire page
            if len(detail_items) == 0:
                print("No items found in content section, searching entire page...")
                detail_items = driver.find_elements(By.CLASS_NAME, "cutsheet-detail-item")
                print(f"Found {len(detail_items)} detail items on entire page")
            
            print("\n--- Extracting All Property Details ---")
            for item in detail_items:
                # Get the full text
                full_text = item.text.strip()
                
                # Skip empty items
                if not full_text:
                    continue
                
                # Try to extract label and value separately
                label = None
                value = None
                
                try:
                    # Find the span element which contains the value
                    span = item.find_element(By.TAG_NAME, "span")
                    value = span.text.strip()
                    
                    # Extract label by removing the value and cleaning up
                    label = full_text.replace(value, "").strip().rstrip(":").strip()
                    
                except:
                    # If span not found, try splitting by colon
                    if ":" in full_text:
                        parts = full_text.split(":", 1)
                        label = parts[0].strip()
                        value = parts[1].strip() if len(parts) > 1 else ""
                    else:
                        # If no colon, use the full text as label
                        label = full_text
                        value = ""
                
                # Store the data if we have a label
                if label:
                    property_data[label] = value
                    print(f"{label}: {value}")
            
            # Print summary
            print(f"\n--- Summary ---")
            print(f"Total fields extracted: {len(property_data)}")
            print(f"\nAll extracted fields:")
            for key, value in sorted(property_data.items()):
                print(f"  • {key}: {value}")
            
            print(f"\nTotal details extracted: {len(property_data)}")
            return property_data
            
        except Exception as e:
            print(f"Error extracting property details: {e}")
            return None
            
    except Exception as e:
        print(f"Error processing cutsheet page: {e}")
        import traceback
        traceback.print_exc()
        return None

def get_mongodb_client():
    """Get MongoDB client connection"""
    if not MONGODB_URI:
        print("Error: MONGODB_URI not set in .env file")
        print("Please set MONGODB_URI in your .env file")
        print("Example for local MongoDB: mongodb://localhost:27017/")
        print("Example for MongoDB Atlas: mongodb+srv://username:password@cluster.mongodb.net/")
        return None
    
    try:
        print(f"Connecting to MongoDB...")
        print(f"URI: {MONGODB_URI.split('@')[0]}@...")  # Hide password in logs
        
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        
        # Test the connection
        client.admin.command('ping')
        print("Connected to MongoDB successfully")
        return client
    except Exception as e:
        error_msg = str(e)
        print(f"\n{'='*60}")
        print("Error connecting to MongoDB:")
        print(f"  {error_msg}")
        print(f"{'='*60}")
        
        if "authentication failed" in error_msg.lower() or "bad auth" in error_msg.lower():
            print("\nAuthentication failed. Please check:")
            print("  1. Your MongoDB username and password in the connection string")
            print("  2. That your user has proper permissions")
            print("  3. For MongoDB Atlas: Check your IP whitelist settings")
            print("\nConnection string format should be:")
            print("  mongodb+srv://username:password@cluster.mongodb.net/")
            print("  or")
            print("  mongodb://username:password@host:port/")
        elif "timeout" in error_msg.lower():
            print("\nConnection timeout. Please check:")
            print("  1. Your internet connection")
            print("  2. MongoDB server is running (for local MongoDB)")
            print("  3. Firewall settings")
        elif "nodename nor servname provided" in error_msg.lower():
            print("\nInvalid hostname. Please check your MongoDB URI")
        
        return None

def save_property_to_mongodb(property_data, client):
    """Save or update property in MongoDB"""
    if not client:
        print("MongoDB client not available")
        return None
    
    try:
        db = client[MONGODB_DB_NAME]
        collection = db[MONGODB_COLLECTION_NAME]
        
        # Use URL as the unique identifier (or PID if available)
        identifier = property_data.get('url') or property_data.get('PID')
        
        if not identifier:
            print("Warning: No identifier found for property, skipping MongoDB save")
            return None
        
        # Check if property exists
        existing_property = collection.find_one({
            "$or": [
                {"url": property_data.get('url')},
                {"PID": property_data.get('PID')}
            ]
        })
        
        current_time = datetime.utcnow()
        
        if existing_property:
            # Property exists - update it
            print(f"Property exists (ID: {existing_property.get('_id')}), updating...")
            
            # Check if price changed
            old_price = existing_property.get('Price')
            new_price = property_data.get('Price')
            price_history = existing_property.get('price_history', [])
            
            if old_price != new_price and old_price:
                print(f"  Price changed: {old_price} -> {new_price}")
                price_history.append({
                    "price": old_price,
                    "date": existing_property.get('date_updated', existing_property.get('date_added'))
                })
            
            # Prepare update data
            update_data = {
                "$set": {
                    **property_data,
                    "date_updated": current_time
                }
            }
            
            # Add price history if it exists
            if price_history:
                update_data["$set"]["price_history"] = price_history
            
            # Update the property
            result = collection.update_one(
                {"_id": existing_property['_id']},
                update_data
            )
            
            if result.modified_count > 0:
                print(f"  Property updated successfully")
                return {"id": existing_property['_id'], "action": "updated"}
            else:
                print(f"  No changes detected")
                return {"id": existing_property['_id'], "action": "no_change"}
        else:
            # New property - insert it
            print(f"New property, adding to database...")
            
            # Add metadata
            property_data['_id'] = ObjectId()
            property_data['date_added'] = current_time
            property_data['date_updated'] = current_time
            
            # Insert the property
            result = collection.insert_one(property_data)
            
            if result.inserted_id:
                print(f"  Property added successfully with ID: {result.inserted_id}")
                return {"id": result.inserted_id, "action": "inserted"}
            else:
                print(f"  Failed to insert property")
                return None
                
    except Exception as e:
        print(f"Error saving to MongoDB: {e}")
        import traceback
        traceback.print_exc()
        return None

def save_properties_to_mongodb(properties):
    """Save all properties to MongoDB"""
    if not properties:
        print("No properties to save to MongoDB")
        return
    
    client = get_mongodb_client()
    if not client:
        print("Cannot save to MongoDB - connection failed")
        return
    
    try:
        print(f"\n{'='*60}")
        print(f"Saving {len(properties)} properties to MongoDB...")
        print(f"{'='*60}")
        
        saved_count = 0
        updated_count = 0
        
        for i, property_data in enumerate(properties, 1):
            print(f"\n[{i}/{len(properties)}] Processing property...")
            result = save_property_to_mongodb(property_data, client)
            if result:
                if result.get('action') == 'inserted':
                    saved_count += 1
                elif result.get('action') == 'updated':
                    updated_count += 1
        
        print(f"\n{'='*60}")
        print(f"MongoDB Save Summary:")
        print(f"  New properties: {saved_count}")
        print(f"  Updated properties: {updated_count}")
        print(f"  Total processed: {len(properties)}")
        print(f"{'='*60}")
        
    finally:
        if client:
            client.close()
            print("MongoDB connection closed")

def main():
    """Main function to run the scraper"""
    if not VIEWPOINT_USER or not VIEWPOINT_PASS:
        print("Error: VIEWPOINT_USER and VIEWPOINT_PASS must be set in .env file")
        return
    
    driver = None
    try:
        print("Starting property scraper...")
        driver = setup_driver()
        
        # Login to viewpoint
        login_to_viewpoint(driver)
        
        # Navigate to new-today-list page
        navigate_to_new_today_list(driver)
        
        # Extract cutsheet links
        cutsheet_links = extract_all_links(driver)
        
        # Visit each cutsheet link and extract property details
        if cutsheet_links:
            print(f"\n{'='*60}")
            print(f"Processing {len(cutsheet_links)} cutsheet links...")
            print(f"{'='*60}")
            
            all_properties = []
            for i, link_data in enumerate(cutsheet_links, 1):
                print(f"\n[{i}/{len(cutsheet_links)}] Processing cutsheet...")
                property_data = extract_property_details(driver, link_data['href'])
                if property_data:
                    property_data['url'] = link_data['href']
                    all_properties.append(property_data)
                time.sleep(1)  # Small delay between pages
            
            print(f"\n{'='*60}")
            print(f"Scraping completed! Processed {len(all_properties)} properties.")
            print(f"{'='*60}")
            
            # Save to MongoDB
            save_properties_to_mongodb(all_properties)
        else:
            print("\nNo cutsheet links found to process.")
        
        print("\nScraping completed successfully!")
        
    except Exception as e:
        print(f"An error occurred: {str(e)}")
        import traceback
        traceback.print_exc()
    
    finally:
        if driver:
            driver.quit()
            print("Browser closed.")

if __name__ == "__main__":
    main()
