package broker

/*
#cgo LDFLAGS: -framework Security -framework CoreFoundation
#cgo CFLAGS: -Wno-deprecated-declarations
#define __STDC_WANT_LIB_EXT1__ 1
#include <Security/Security.h>
#include <CoreFoundation/CoreFoundation.h>
#include <unistd.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>

// Private (SecKeychainItemPriv.h) but exported; security's set-generic-password-partition-list uses it.
OSStatus SecKeychainItemSetAccessWithPassword(SecKeychainItemRef itemRef, SecAccessRef accessRef, UInt32 passwordLength, const void *password);
// Private (SecTrustedApplicationPriv.h) but exported; NULL requirement when none is recorded.
OSStatus SecTrustedApplicationCopyRequirement(SecTrustedApplicationRef appRef, SecRequirementRef *requirement);

static OSStatus signed_runtime(void) {
    SecCodeRef code = NULL;
    CFDictionaryRef info = NULL;
    OSStatus s = SecCodeCopySelf(kSecCSDefaultFlags, &code);
    if (s == errSecSuccess) s = SecCodeCheckValidity(code, kSecCSStrictValidate, NULL);
    if (s == errSecSuccess) s = SecCodeCopySigningInformation(code, kSecCSSigningInformation, &info);
    uint32_t flags = 0;
    if (s == errSecSuccess) {
        CFNumberRef n = CFDictionaryGetValue(info, kSecCodeInfoFlags);
        if (!n || !CFNumberGetValue(n, kCFNumberSInt32Type, &flags) || !(flags & kSecCodeSignatureRuntime)) s = errSecCSUnsigned;
    }
    if (info) CFRelease(info);
    if (code) CFRelease(code);
    return s;
}
// The login keychain is opened by path so a user-set default keychain cannot redirect the item.
// A NULL account matches every account of the service.
static CFMutableDictionaryRef item_query(const char *keychain_path, const char *service, const char *account, SecKeychainRef *keychain, OSStatus *status) {
    *status = SecKeychainOpen(keychain_path, keychain);
    if (*status != errSecSuccess) return NULL;
    CFMutableDictionaryRef q = CFDictionaryCreateMutable(NULL, 0, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    CFDictionarySetValue(q, kSecClass, kSecClassGenericPassword);
    CFStringRef s = CFStringCreateWithCString(NULL, service, kCFStringEncodingUTF8);
    CFDictionarySetValue(q, kSecAttrService, s);
    CFRelease(s);
    if (account) {
        s = CFStringCreateWithCString(NULL, account, kCFStringEncodingUTF8);
        CFDictionarySetValue(q, kSecAttrAccount, s);
        CFRelease(s);
    }
    CFArrayRef list = CFArrayCreate(NULL, (const void **)keychain, 1, &kCFTypeArrayCallBacks);
    CFDictionarySetValue(q, kSecMatchSearchList, list);
    CFRelease(list);
    return q;
}
// Attributes only, so it never prompts; *account is the first item's, malloc'd, empty when it has none.
static OSStatus item_accounts(const char *path, const char *service, long *count, char **account) {
    SecKeychainRef kc = NULL;
    OSStatus s;
    CFMutableDictionaryRef q = item_query(path, service, NULL, &kc, &s);
    if (!q) return s;
    CFDictionarySetValue(q, kSecReturnAttributes, kCFBooleanTrue);
    CFDictionarySetValue(q, kSecMatchLimit, kSecMatchLimitAll);
    CFTypeRef result = NULL;
    s = SecItemCopyMatching(q, &result);
    if (s == errSecSuccess) {
        *count = CFArrayGetCount((CFArrayRef)result);
        CFStringRef a = *count ? CFDictionaryGetValue(CFArrayGetValueAtIndex((CFArrayRef)result, 0), kSecAttrAccount) : NULL;
        if (a && CFGetTypeID(a) != CFStringGetTypeID()) a = NULL;
        CFIndex size = a ? CFStringGetMaximumSizeForEncoding(CFStringGetLength(a), kCFStringEncodingUTF8) + 1 : 1;
        *account = calloc(1, size);
        if (!*account) s = errSecAllocate;
        else if (a && !CFStringGetCString(a, *account, size, kCFStringEncodingUTF8)) s = errSecDecode;
    }
    if (result) CFRelease(result);
    CFRelease(q);
    CFRelease(kc);
    return s;
}
// no_ui: fail rather than ask (errSecInteractionNotAllowed), for a broker-only item a since-reinstalled broker can no
// longer read.
static OSStatus item_get(const char *path, const char *service, const char *account, int no_ui, void **bytes, long *length) {
    SecKeychainRef kc = NULL;
    OSStatus s;
    CFMutableDictionaryRef q = item_query(path, service, account, &kc, &s);
    if (!q) return s;
    CFDictionarySetValue(q, kSecReturnData, kCFBooleanTrue);
    if (no_ui) CFDictionarySetValue(q, kSecUseAuthenticationUI, kSecUseAuthenticationUIFail);
    CFTypeRef result = NULL;
    s = SecItemCopyMatching(q, &result);
    if (s == errSecSuccess) {
        *length = CFDataGetLength((CFDataRef)result);
        *bytes = malloc(*length);
        if (!*bytes) s = errSecAllocate;
        else memcpy(*bytes, CFDataGetBytePtr((CFDataRef)result), *length);
    }
    if (result) CFRelease(result);
    CFRelease(q);
    CFRelease(kc);
    return s;
}
// An access whose only trusted application is this executable.
static OSStatus self_access(const char *label, SecAccessRef *access) {
    SecTrustedApplicationRef self = NULL;
    OSStatus s = SecTrustedApplicationCreateFromPath(NULL, &self);
    if (s != errSecSuccess) return s;
    const void *apps[] = {self};
    CFArrayRef list = CFArrayCreate(NULL, apps, 1, &kCFTypeArrayCallBacks);
    CFStringRef description = CFStringCreateWithCString(NULL, label, kCFStringEncodingUTF8);
    s = SecAccessCreate(description, list, access);
    CFRelease(description);
    CFRelease(list);
    CFRelease(self);
    return s;
}
// A broker-only item. An existing one is deleted, never updated, so a pre-planted permissive ACL does not survive.
static OSStatus secret_put(const char *path, const char *service, const char *account, const char *label, const void *bytes, long length) {
    SecKeychainRef kc = NULL;
    OSStatus s;
    CFMutableDictionaryRef q = item_query(path, service, account, &kc, &s);
    if (!q) return s;
    s = SecItemDelete(q);
    if (s != errSecSuccess && s != errSecItemNotFound) goto done;
    SecAccessRef access = NULL;
    s = self_access(label, &access);
    if (s != errSecSuccess) goto done;
    CFDictionaryRemoveValue(q, kSecMatchSearchList);
    CFDataRef data = CFDataCreate(NULL, bytes, length);
    CFDictionarySetValue(q, kSecUseKeychain, kc);
    CFDictionarySetValue(q, kSecValueData, data);
    CFDictionarySetValue(q, kSecAttrAccess, access);
    s = SecItemAdd(q, NULL);
    CFRelease(data);
    CFRelease(access);
done:
    CFRelease(q);
    CFRelease(kc);
    return s;
}
static OSStatus secret_delete(const char *path, const char *service, const char *account) {
    SecKeychainRef kc = NULL;
    OSStatus s;
    CFMutableDictionaryRef q = item_query(path, service, account, &kc, &s);
    if (!q) return s;
    CFDictionarySetValue(q, kSecUseAuthenticationUI, kSecUseAuthenticationUIFail);
    s = SecItemDelete(q);
    CFRelease(q);
    CFRelease(kc);
    return s;
}
static OSStatus codex_create(const char *path, const char *service, const char *account, const char *label) {
    SecKeychainRef kc = NULL;
    OSStatus s;
    CFMutableDictionaryRef q = item_query(path, service, account, &kc, &s);
    if (!q) return s;
    SecAccessRef access = NULL;
    s = self_access(label, &access);
    if (s == errSecSuccess) {
        CFDictionaryRemoveValue(q, kSecMatchSearchList);
        CFDictionarySetValue(q, kSecUseKeychain, kc);
        CFDictionarySetValue(q, kSecAttrAccess, access);
        CFDataRef data = CFDataCreate(NULL, (const UInt8 *)"pending", 7);
        CFDictionarySetValue(q, kSecValueData, data);
        s = SecItemAdd(q, NULL);
        CFRelease(data);
    }
    if (access) CFRelease(access);
    CFRelease(q);
    CFRelease(kc);
    return s;
}
static OSStatus codex_write(const char *path, const char *service, const char *account, const void *bytes, long length) {
    SecKeychainRef kc = NULL;
    OSStatus s;
    CFMutableDictionaryRef q = item_query(path, service, account, &kc, &s);
    if (!q) return s;
    CFDictionarySetValue(q, kSecUseAuthenticationUI, kSecUseAuthenticationUIFail);
    CFDataRef data = CFDataCreate(NULL, bytes, length);
    const void *keys[] = {kSecValueData}, *values[] = {data};
    CFDictionaryRef attrs = CFDictionaryCreate(NULL, keys, values, 1, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    s = SecItemUpdate(q, attrs);
    CFRelease(attrs);
    CFRelease(data);
    CFRelease(q);
    CFRelease(kc);
    return s;
}
static OSStatus item_ref(const char *path, const char *service, const char *account, SecKeychainItemRef *item) {
    SecKeychainRef kc = NULL;
    OSStatus s;
    CFMutableDictionaryRef q = item_query(path, service, account, &kc, &s);
    if (!q) return s;
    CFDictionarySetValue(q, kSecReturnRef, kCFBooleanTrue);
    s = SecItemCopyMatching(q, (CFTypeRef *)item);
    CFRelease(q);
    CFRelease(kc);
    return s;
}
// The partition ID securityd gives items this executable creates: its team, else its cdhash in lowercase hex.
static OSStatus self_partition(CFStringRef *id) {
    SecCodeRef code = NULL;
    CFDictionaryRef info = NULL;
    OSStatus s = SecCodeCopySelf(kSecCSDefaultFlags, &code);
    if (s == errSecSuccess) s = SecCodeCopySigningInformation(code, kSecCSSigningInformation, &info);
    if (s == errSecSuccess) {
        CFStringRef team = CFDictionaryGetValue(info, kSecCodeInfoTeamIdentifier);
        CFDataRef cdhash = CFDictionaryGetValue(info, kSecCodeInfoUnique);
        if (team) *id = CFStringCreateWithFormat(NULL, NULL, CFSTR("teamid:%@"), team);
        else if (cdhash) {
            CFMutableStringRef hex = CFStringCreateMutable(NULL, 0);
            CFStringAppend(hex, CFSTR("cdhash:"));
            for (CFIndex i = 0; i < CFDataGetLength(cdhash); i++) CFStringAppendFormat(hex, NULL, CFSTR("%02x"), CFDataGetBytePtr(cdhash)[i]);
            *id = hex;
        } else s = errSecCSUnsigned;
    }
    if (info) CFRelease(info);
    if (code) CFRelease(code);
    return s;
}
static CFStringRef auth_name(CFStringRef a) {
    if (CFEqual(a, kSecACLAuthorizationAny)) return CFSTR("any");
    if (CFEqual(a, kSecACLAuthorizationDecrypt)) return CFSTR("decrypt");
    if (CFEqual(a, kSecACLAuthorizationExportClear)) return CFSTR("export_clear");
    if (CFEqual(a, kSecACLAuthorizationExportWrapped)) return CFSTR("export_wrapped");
    if (CFEqual(a, kSecACLAuthorizationPartitionID)) return CFSTR("partition_id");
    if (CFEqual(a, kSecACLAuthorizationEncrypt)) return CFSTR("encrypt");
    if (CFEqual(a, kSecACLAuthorizationDerive)) return CFSTR("derive");
    if (CFEqual(a, kSecACLAuthorizationSign)) return CFSTR("sign");
    if (CFEqual(a, kSecACLAuthorizationMAC)) return CFSTR("mac");
    if (CFEqual(a, kSecACLAuthorizationChangeACL)) return CFSTR("change_acl");
    if (CFEqual(a, kSecACLAuthorizationChangeOwner)) return CFSTR("change_owner");
    if (CFEqual(a, kSecACLAuthorizationIntegrity)) return CFSTR("integrity");
    if (CFEqual(a, kSecACLAuthorizationDelete)) return CFSTR("delete");
    return a;
}
static int reads(CFArrayRef auths) {
    for (CFIndex i = 0; i < CFArrayGetCount(auths); i++) {
        CFStringRef n = auth_name(CFArrayGetValueAtIndex(auths, i));
        if (CFEqual(n, CFSTR("any")) || CFEqual(n, CFSTR("decrypt")) || CFEqual(n, CFSTR("export_clear")) || CFEqual(n, CFSTR("export_wrapped"))) return 1;
    }
    return 0;
}
static int partition_entry(CFArrayRef auths) {
    return CFArrayContainsValue(auths, CFRangeMake(0, CFArrayGetCount(auths)), kSecACLAuthorizationPartitionID);
}
// A partition entry's description is the hex of an XML plist {"Partitions": [ID...]}, as security's
// set-generic-password-partition-list writes it.
static CFStringRef partitions_description(CFStringRef id) {
    const void *ids[] = {id};
    CFArrayRef list = CFArrayCreate(NULL, ids, 1, &kCFTypeArrayCallBacks);
    const void *keys[] = {CFSTR("Partitions")}, *values[] = {list};
    CFDictionaryRef plist = CFDictionaryCreate(NULL, keys, values, 1, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    CFDataRef xml = CFPropertyListCreateData(NULL, plist, kCFPropertyListXMLFormat_v1_0, 0, NULL);
    CFMutableStringRef hex = NULL;
    if (xml) {
        hex = CFStringCreateMutable(NULL, 0);
        for (CFIndex i = 0; i < CFDataGetLength(xml); i++) CFStringAppendFormat(hex, NULL, CFSTR("%02x"), CFDataGetBytePtr(xml)[i]);
        CFRelease(xml);
    }
    CFRelease(plist);
    CFRelease(list);
    return hex;
}
// Edits the item's own entries in place: setting an access applies only changed entries, one at a time, so a
// fresh SecAccessCreate access would be appended beside the old ones. Either the entries that read or export the
// item get only this executable, or the partition list gets only its partition ID. With the keychain password
// (as security's set-generic-password-partition-list does) securityd skips its owner check; without one (NULL,
// read and export entries only) SecKeychainItemSetAccess asks by dialog per entry, which securityd refuses for
// the partition list.
static OSStatus item_trust_self(const char *path, const char *service, const char *account, int partition_list, const void *password, long length) {
    SecKeychainItemRef item = NULL;
    SecAccessRef access = NULL;
    SecTrustedApplicationRef self = NULL;
    CFStringRef partition = NULL, partitions = NULL;
    CFArrayRef apps = NULL, acls = NULL;
    OSStatus s = item_ref(path, service, account, &item);
    if (s == errSecSuccess) s = SecKeychainItemCopyAccess(item, &access);
    if (s == errSecSuccess) s = SecTrustedApplicationCreateFromPath(NULL, &self);
    if (s == errSecSuccess) s = self_partition(&partition);
    if (s == errSecSuccess && !(partitions = partitions_description(partition))) s = errSecAllocate;
    if (s == errSecSuccess) {
        const void *list[] = {self};
        apps = CFArrayCreate(NULL, list, 1, &kCFTypeArrayCallBacks);
        s = SecAccessCopyACLList(access, &acls);
    }
    for (CFIndex i = 0; s == errSecSuccess && i < CFArrayGetCount(acls); i++) {
        SecACLRef acl = (SecACLRef)CFArrayGetValueAtIndex(acls, i);
        CFArrayRef auths = SecACLCopyAuthorizations(acl), old = NULL;
        CFStringRef description = NULL;
        SecKeychainPromptSelector selector = 0;
        if (!auths) s = errSecDecode;
        if (s == errSecSuccess) s = SecACLCopyContents(acl, &old, &description, &selector);
        if (s == errSecSuccess && partition_entry(auths)) {
            if (partition_list) s = SecACLSetContents(acl, old, partitions, selector);
        } else if (s == errSecSuccess && reads(auths) && !partition_list) s = SecACLSetContents(acl, apps, description, selector);
        if (old) CFRelease(old);
        if (description) CFRelease(description);
        if (auths) CFRelease(auths);
    }
    if (s == errSecSuccess) s = password ? SecKeychainItemSetAccessWithPassword(item, access, (UInt32)length, password) : SecKeychainItemSetAccess(item, access);
    if (acls) CFRelease(acls);
    if (apps) CFRelease(apps);
    if (partitions) CFRelease(partitions);
    if (partition) CFRelease(partition);
    if (self) CFRelease(self);
    if (access) CFRelease(access);
    if (item) CFRelease(item);
    return s;
}
static void put_hex(CFMutableDataRef out, const UInt8 *b, CFIndex n) {
    static const char digits[] = "0123456789abcdef";
    CFDataAppendBytes(out, (const UInt8 *)" ", 1);
    for (CFIndex i = 0; i < n; i++) {
        UInt8 pair[] = {digits[b[i] >> 4], digits[b[i] & 15]};
        CFDataAppendBytes(out, pair, 2);
    }
}
static void put_string(CFMutableDataRef out, CFStringRef s) {
    CFDataRef d = CFStringCreateExternalRepresentation(NULL, s, kCFStringEncodingUTF8, '?');
    put_hex(out, d ? CFDataGetBytePtr(d) : NULL, d ? CFDataGetLength(d) : 0);
    if (d) CFRelease(d);
}
static void put_record(CFMutableDataRef out, const char *kind) {
    if (CFDataGetLength(out)) CFDataAppendBytes(out, (const UInt8 *)"\n", 1);
    CFDataAppendBytes(out, (const UInt8 *)kind, strlen(kind));
}
// A kind record of the application's path (its data, NUL-terminated) and its code requirement, empty when it
// records none: a rebuilt broker keeps its path but not its requirement (an ad hoc signature's is its cdhash).
static OSStatus put_app(CFMutableDataRef out, const char *kind, SecTrustedApplicationRef app) {
    CFDataRef d = NULL;
    SecRequirementRef requirement = NULL;
    CFStringRef text = NULL;
    OSStatus s = SecTrustedApplicationCopyData(app, &d);
    if (s == errSecSuccess) s = SecTrustedApplicationCopyRequirement(app, &requirement);
    if (s == errSecSuccess && requirement) s = SecRequirementCopyString(requirement, kSecCSDefaultFlags, &text);
    if (s == errSecSuccess) {
        CFIndex n = CFDataGetLength(d);
        while (n && CFDataGetBytePtr(d)[n - 1] == 0) n--;
        put_record(out, kind);
        put_hex(out, CFDataGetBytePtr(d), n);
        put_string(out, text ? text : CFSTR(""));
    }
    if (text) CFRelease(text);
    if (requirement) CFRelease(requirement);
    if (d) CFRelease(d);
    return s;
}
static int nibble(UniChar c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}
static OSStatus put_partitions(CFMutableDataRef out, CFStringRef description) {
    CFIndex n = description ? CFStringGetLength(description) : 0;
    if (n % 2) return errSecDecode;
    CFMutableDataRef xml = CFDataCreateMutable(NULL, n / 2);
    OSStatus s = errSecSuccess;
    for (CFIndex i = 0; s == errSecSuccess && i < n; i += 2) {
        int high = nibble(CFStringGetCharacterAtIndex(description, i)), low = nibble(CFStringGetCharacterAtIndex(description, i + 1));
        if (high < 0 || low < 0) s = errSecDecode;
        else {
            UInt8 b = (UInt8)(high << 4 | low);
            CFDataAppendBytes(xml, &b, 1);
        }
    }
    CFPropertyListRef plist = s == errSecSuccess ? CFPropertyListCreateWithData(NULL, xml, kCFPropertyListImmutable, NULL, NULL) : NULL;
    CFArrayRef ids = plist && CFGetTypeID(plist) == CFDictionaryGetTypeID() ? CFDictionaryGetValue(plist, CFSTR("Partitions")) : NULL;
    if (!ids || CFGetTypeID(ids) != CFArrayGetTypeID()) s = errSecDecode;
    for (CFIndex i = 0; s == errSecSuccess && i < CFArrayGetCount(ids); i++) {
        CFStringRef id = CFArrayGetValueAtIndex(ids, i);
        if (CFGetTypeID(id) != CFStringGetTypeID()) s = errSecDecode;
        else {
            put_record(out, "partition");
            put_string(out, id);
        }
    }
    if (plist) CFRelease(plist);
    CFRelease(xml);
    return s;
}
// The item's access in the record text parseAccess reads; attributes only, so it never prompts.
static OSStatus item_access(const char *path, const char *service, const char *account, void **text, long *length) {
    SecKeychainItemRef item = NULL;
    SecAccessRef access = NULL;
    SecTrustedApplicationRef self = NULL;
    CFStringRef partition = NULL;
    CFArrayRef acls = NULL;
    CFMutableDataRef out = CFDataCreateMutable(NULL, 0);
    OSStatus s = item_ref(path, service, account, &item);
    if (s == errSecSuccess) s = SecKeychainItemCopyAccess(item, &access);
    if (s == errSecSuccess) s = SecTrustedApplicationCreateFromPath(NULL, &self);
    if (s == errSecSuccess) s = self_partition(&partition);
    if (s == errSecSuccess) s = put_app(out, "self", self);
    if (s == errSecSuccess) put_string(out, partition);
    if (s == errSecSuccess) s = SecAccessCopyACLList(access, &acls);
    for (CFIndex i = 0; s == errSecSuccess && i < CFArrayGetCount(acls); i++) {
        SecACLRef acl = (SecACLRef)CFArrayGetValueAtIndex(acls, i);
        CFArrayRef auths = SecACLCopyAuthorizations(acl), list = NULL;
        CFStringRef description = NULL;
        SecKeychainPromptSelector selector = 0;
        if (!auths) s = errSecDecode;
        if (s == errSecSuccess) s = SecACLCopyContents(acl, &list, &description, &selector);
        if (s == errSecSuccess) {
            put_record(out, "acl");
            for (CFIndex j = 0; j < CFArrayGetCount(auths); j++) put_string(out, auth_name(CFArrayGetValueAtIndex(auths, j)));
            if (!list) put_record(out, "any");
            for (CFIndex j = 0; list && s == errSecSuccess && j < CFArrayGetCount(list); j++)
                s = put_app(out, "app", (SecTrustedApplicationRef)CFArrayGetValueAtIndex(list, j));
        }
        if (s == errSecSuccess && partition_entry(auths)) s = put_partitions(out, description);
        if (list) CFRelease(list);
        if (description) CFRelease(description);
        if (auths) CFRelease(auths);
    }
    if (s == errSecSuccess) {
        *length = CFDataGetLength(out);
        *text = malloc(*length ? *length : 1);
        if (!*text) s = errSecAllocate;
        else memcpy(*text, CFDataGetBytePtr(out), *length);
    }
    CFRelease(out);
    if (acls) CFRelease(acls);
    if (partition) CFRelease(partition);
    if (self) CFRelease(self);
    if (access) CFRelease(access);
    if (item) CFRelease(item);
    return s;
}
static int hidden_token(char *buffer, size_t capacity) {
    struct termios old, hidden;
    if (tcgetattr(STDIN_FILENO, &old) != 0) return -1;
    hidden = old;
    hidden.c_lflag &= ~(ECHO | ECHONL);
    if (tcsetattr(STDIN_FILENO, TCSAFLUSH, &hidden) != 0) return -1;
    size_t n = 0;
    char c;
    int result = -1;
    while (read(STDIN_FILENO, &c, 1) == 1) {
        if (c == '\n' || c == '\r') { result = (int)n; break; }
        if (n + 1 >= capacity) break;
        buffer[n++] = c;
    }
    tcsetattr(STDIN_FILENO, TCSAFLUSH, &old);
    memset_s(&c, sizeof c, 0, sizeof c); // on overflow c holds a byte of the secret
    return result;
}
*/
import "C"

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"unsafe"
)

// CheckInstalled refuses credential access unless this is the root-owned, hardened-runtime install.
func CheckInstalled() error {
	path, err := os.Executable()
	if err != nil || path != InstalledPath {
		return fmt.Errorf("credential access requires %s; install: %s", InstalledPath, UpgradeCommand)
	}
	for p := path; ; p = filepath.Dir(p) {
		info, err := os.Lstat(p)
		if err != nil {
			return err
		}
		stat, ok := info.Sys().(*syscall.Stat_t)
		if !ok || stat.Uid != 0 || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm()&0o022 != 0 {
			return fmt.Errorf("unsafe broker installation: %s", p)
		}
		if p == "/" {
			break
		}
	}
	if s := C.signed_runtime(); s != 0 {
		return fmt.Errorf("broker signature lacks hardened runtime (status %d); install: %s", s, UpgradeCommand)
	}
	return nil
}

// ReadHidden reads one line from the terminal with echo off.
func ReadHidden() ([]byte, error) {
	buf := make([]byte, maxToken+2)
	n := C.hidden_token((*C.char)(unsafe.Pointer(&buf[0])), C.size_t(len(buf)))
	if n < 0 {
		erase(buf)
		return nil, errors.New("needs an interactive terminal")
	}
	return buf[:int(n)], nil
}

func status(s C.OSStatus) error {
	switch s {
	case 0:
		return nil
	case C.errSecItemNotFound:
		return ErrNotFound
	case C.errSecAuthFailed:
		return ErrAuthFailed
	case C.errSecInteractionNotAllowed:
		return ErrNeedsDialog
	case C.errSecInvalidOwnerEdit:
		return ErrInvalidOwnerEdit
	}
	return fmt.Errorf("keychain status %d", int(s))
}

// fileKeychain is the keychain file at this path, with no install check.
type fileKeychain string

// cStrings converts s to C strings for one call; free releases them.
func cStrings(s ...string) (ps []*C.char, free func()) {
	for _, v := range s {
		ps = append(ps, C.CString(v))
	}
	return ps, func() {
		for _, p := range ps {
			C.free(unsafe.Pointer(p))
		}
	}
}

func (k fileKeychain) get(service, account string, noUI bool) ([]byte, error) {
	p, free := cStrings(string(k), service, account)
	defer free()
	var data unsafe.Pointer
	var n C.long
	var ui C.int
	if noUI {
		ui = 1
	}
	if err := status(C.item_get(p[0], p[1], p[2], ui, &data, &n)); err != nil {
		return nil, err
	}
	defer C.free(data)
	b := C.GoBytes(data, C.int(n))
	C.memset(data, 0, C.size_t(n))
	return b, nil
}

func (k fileKeychain) Get(account string) ([]byte, error) { return k.get(tokenService, account, true) }

func (k fileKeychain) GetWithDialog(account string) ([]byte, error) {
	return k.get(tokenService, account, false)
}

func (k fileKeychain) Put(account string, token []byte) error {
	if len(token) == 0 {
		return errors.New("empty setup-token")
	}
	return k.put(tokenService, account, "ensemble Claude setup-token "+account, token)
}

func (k fileKeychain) put(service, account, label string, data []byte) error {
	p, free := cStrings(string(k), service, account, label)
	defer free()
	return status(C.secret_put(p[0], p[1], p[2], p[3], unsafe.Pointer(&data[0]), C.long(len(data))))
}

func (k fileKeychain) Secret(service, account string) ([]byte, error) {
	return k.get(service, account, true)
}

func (k fileKeychain) PutSecret(service, account string, data []byte) error {
	if len(data) == 0 {
		return errors.New("empty secret")
	}
	return k.put(service, account, "ensemble "+service, data)
}

func (k fileKeychain) DeleteSecret(service, account string) error {
	p, free := cStrings(string(k), service, account)
	defer free()
	if err := status(C.secret_delete(p[0], p[1], p[2])); !errors.Is(err, ErrNotFound) {
		return err
	}
	return nil
}

func (k fileKeychain) AppAccount(app string) (string, error) {
	p, free := cStrings(string(k), app)
	defer free()
	var n C.long
	var account *C.char
	err := status(C.item_accounts(p[0], p[1], &n, &account))
	defer C.free(unsafe.Pointer(account))
	switch {
	case err != nil:
		return "", err
	case n > 1:
		return "", ErrAmbiguous
	}
	return C.GoString(account), nil
}

func (k fileKeychain) AppKey(app, account string) ([]byte, error) { return k.get(app, account, false) }

func (k fileKeychain) TrustApps(app, account string, password []byte) error {
	return k.trust(app, account, false, password)
}

func (k fileKeychain) TrustAppsByDialog(app, account string) error {
	p, free := cStrings(string(k), app, account)
	defer free()
	return status(C.item_trust_self(p[0], p[1], p[2], 0, nil, 0))
}

func (k fileKeychain) TrustPartition(app, account string, password []byte) error {
	return k.trust(app, account, true, password)
}

// trust runs item_trust_self with the keychain password: the partition list when partitionList, else the read
// and export entries.
func (k fileKeychain) trust(app, account string, partitionList bool, password []byte) error {
	if len(password) == 0 {
		return errors.New("empty keychain password")
	}
	p, free := cStrings(string(k), app, account)
	defer free()
	var list C.int
	if partitionList {
		list = 1
	}
	return status(C.item_trust_self(p[0], p[1], p[2], list, unsafe.Pointer(&password[0]), C.long(len(password))))
}

func (k fileKeychain) AppAccess(app, account string) (AppAccess, error) {
	p, free := cStrings(string(k), app, account)
	defer free()
	var text unsafe.Pointer
	var n C.long
	if err := status(C.item_access(p[0], p[1], p[2], &text, &n)); err != nil {
		return AppAccess{}, err
	}
	defer C.free(text)
	return parseAccess(C.GoBytes(text, C.int(n)))
}

// SystemKeychain is the item in the account's login keychain, reachable only from the hardened install.
type SystemKeychain struct{ Home string }

func (k SystemKeychain) login() fileKeychain {
	return fileKeychain(filepath.Join(k.Home, "Library/Keychains/login.keychain-db"))
}

func (k SystemKeychain) Get(account string) ([]byte, error) {
	if err := CheckInstalled(); err != nil {
		return nil, err
	}
	return k.login().Get(account)
}

func (k SystemKeychain) GetWithDialog(account string) ([]byte, error) {
	if err := CheckInstalled(); err != nil {
		return nil, err
	}
	return k.login().GetWithDialog(account)
}

func (k SystemKeychain) Put(account string, token []byte) error {
	if err := CheckInstalled(); err != nil {
		return err
	}
	return k.login().Put(account, token)
}

func (k SystemKeychain) AppAccount(app string) (string, error) {
	if err := CheckInstalled(); err != nil {
		return "", err
	}
	return k.login().AppAccount(app)
}

func (k SystemKeychain) AppKey(app, account string) ([]byte, error) {
	if err := CheckInstalled(); err != nil {
		return nil, err
	}
	return k.login().AppKey(app, account)
}

func (k SystemKeychain) TrustApps(app, account string, password []byte) error {
	if err := CheckInstalled(); err != nil {
		return err
	}
	return k.login().TrustApps(app, account, password)
}

func (k SystemKeychain) TrustAppsByDialog(app, account string) error {
	if err := CheckInstalled(); err != nil {
		return err
	}
	return k.login().TrustAppsByDialog(app, account)
}

func (k SystemKeychain) TrustPartition(app, account string, password []byte) error {
	if err := CheckInstalled(); err != nil {
		return err
	}
	return k.login().TrustPartition(app, account, password)
}

func (k SystemKeychain) AppAccess(app, account string) (AppAccess, error) {
	if err := CheckInstalled(); err != nil {
		return AppAccess{}, err
	}
	return k.login().AppAccess(app, account)
}

func (k SystemKeychain) Secret(service, account string) ([]byte, error) {
	if err := CheckInstalled(); err != nil {
		return nil, err
	}
	return k.login().Secret(service, account)
}

func (k SystemKeychain) PutSecret(service, account string, data []byte) error {
	if err := CheckInstalled(); err != nil {
		return err
	}
	return k.login().PutSecret(service, account, data)
}

func (k SystemKeychain) DeleteSecret(service, account string) error {
	if err := CheckInstalled(); err != nil {
		return err
	}
	return k.login().DeleteSecret(service, account)
}

func (k SystemKeychain) CreateCodex() error {
	if err := CheckInstalled(); err != nil {
		return err
	}
	p, free := cStrings(string(k.login()), codexService, codexAccount, "ensemble Codex login")
	defer free()
	return status(C.codex_create(p[0], p[1], p[2], p[3]))
}

func (k SystemKeychain) WriteCodex(data []byte) error {
	if err := CheckInstalled(); err != nil {
		return err
	}
	if len(data) == 0 || len(data) > maxCodexAuth {
		return errors.New("invalid Codex credential size")
	}
	p, free := cStrings(string(k.login()), codexService, codexAccount)
	defer free()
	return status(C.codex_write(p[0], p[1], p[2], unsafe.Pointer(&data[0]), C.long(len(data))))
}
